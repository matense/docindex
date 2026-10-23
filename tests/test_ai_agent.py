import io
import json
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import ChatConversation, Drive, StoredFile, User
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
    assert events[1]["type"] == "step"
    assert events[1]["step"]["label"] == "Searched files"
    # tool_start is announced before the tool runs (live working chip).
    assert events[0]["type"] == "tool_start"
    assert events[0]["step"]["label"] == "Searching files"
    assert events[-1]["type"] == "answer"
    assert "tomato soup" in events[-1]["answer"]

    with app.app_context():
        conv = ChatConversation.query.one()
        roles = [m.role for m in conv.messages]
        assert roles == ["user", "step", "assistant"]


def test_chat_endpoint_disabled_ai(auth_client):
    resp = auth_client.post("/ai/chat", json={"message": "hello"})
    assert resp.status_code == 503


def test_list_drives_tool(app, user):
    from app.models import Drive
    with app.app_context():
        drive = Drive(name="Personal", user_id=user)
        db.session.add(drive)
        db.session.commit()
        fid = _make_indexed_file(app, "drivefile.txt", "some content")
        stored = db.session.get(StoredFile, fid)
        stored.drive_id = drive.id
        db.session.commit()
        u = db.session.get(User, user)
        result = agent_service._tool_list_drives(u)
    assert result["total"] == 1
    entry = result["drives"][0]
    assert entry["name"] == "Personal" and entry["files"] == 1
    assert entry["read_only"] is False
    assert result["scoped_to"] is None  # no drive passed -> not scoped

    with app.app_context():
        u = db.session.get(User, user)
        drive = Drive.query.filter_by(user_id=user).one()
        scoped = agent_service._tool_list_drives(u, drive)
        assert scoped["scoped_to"] == "Personal"


def test_move_and_copy_files_tools(app, user):
    from app.models import Drive
    fid = _make_indexed_file(app, "moveme.txt", "move me content")
    with app.app_context():
        src = Drive(name="Source", user_id=user)
        dst = Drive(name="Dest", user_id=user)
        db.session.add_all([src, dst])
        db.session.commit()
        db.session.get(StoredFile, fid).drive_id = src.id
        db.session.commit()
        src_id, dst_id = src.id, dst.id
    with app.app_context():
        u = db.session.get(User, user)
        out = agent_service.TOOL_HANDLERS["move_files"](
            u, {"file_ids": [fid], "drive_id": dst_id}, None)
        assert out == {"moved": 1, "skipped": 0, "details": out["details"]}
        assert db.session.get(StoredFile, fid).drive_id == dst_id
        out = agent_service.TOOL_HANDLERS["copy_files"](
            u, {"file_ids": [fid], "drive_id": src_id}, None)
        assert out["copied"] == 1 and out["skipped"] == 0
        copies = StoredFile.query.filter_by(name="moveme.txt").all()
        assert {c.drive_id for c in copies} == {src_id, dst_id}


def test_move_files_tool_refuses_synced_drive(app, user):
    from app.models import Drive
    fid = _make_indexed_file(app, "locked.txt", "locked content")
    with app.app_context():
        u = db.session.get(User, user)
        synced = Drive(name="Synced", user_id=user,
                       source_path="/tmp/synced-drive")
        db.session.add(synced)
        db.session.commit()
        out = agent_service.TOOL_HANDLERS["move_files"](
            u, {"file_ids": [fid], "drive_id": synced.id}, None)
        assert "error" in out
        out = agent_service.TOOL_HANDLERS["copy_files"](
            u, {"file_ids": [fid], "drive_id": 999999}, None)
        assert "error" in out


def test_create_and_delete_folder_tools(app, user):
    from app.models import Drive, Folder
    with app.app_context():
        u = db.session.get(User, user)
        drive = Drive(name="Personal", user_id=user)
        db.session.add(drive)
        db.session.commit()
        did = drive.id
        out = agent_service.TOOL_HANDLERS["create_folder"](
            u, {"name": "Reports", "drive_id": did}, None)
        assert out["name"] == "Reports" and out["drive_id"] == did
        assert out["parent_id"] is None
        sub = agent_service.TOOL_HANDLERS["create_folder"](
            u, {"name": "2026", "drive_id": did,
                "parent_id": out["folder_id"]}, None)
        assert sub["parent_id"] == out["folder_id"]
        # duplicate name auto-renamed
        dup = agent_service.TOOL_HANDLERS["create_folder"](
            u, {"name": "Reports", "drive_id": did}, None)
        assert dup["name"] != "Reports"
        assert Folder.query.filter_by(drive_id=did).count() == 3
        out = agent_service.TOOL_HANDLERS["delete_folder"](
            u, {"folder_id": out["folder_id"]}, None)
        assert out == {"deleted": True, "folder_id": out["folder_id"],
                      "files_trashed": 0}
        assert Folder.query.filter_by(drive_id=did).count() == 1
        assert db.session.get(Folder, out["folder_id"]) is None


def test_folder_tools_guardrails(app, user):
    from app.models import Drive, Folder
    fid = _make_indexed_file(app, "keep.txt", "keep me")
    with app.app_context():
        u = db.session.get(User, user)
        drive = Drive(name="Personal", user_id=user)
        synced = Drive(name="Synced", user_id=user,
                       source_path="/tmp/synced-drive")
        db.session.add_all([drive, synced])
        db.session.commit()
        did, sid = drive.id, synced.id
        # files inside a deleted folder go to the trash, not purged
        folder = Folder(name="Doomed", user_id=user, drive_id=did)
        db.session.add(folder)
        db.session.commit()
        db.session.get(StoredFile, fid).folder_id = folder.id
        db.session.get(StoredFile, fid).drive_id = did
        db.session.commit()
        folder_id = folder.id
        out = agent_service.TOOL_HANDLERS["delete_folder"](
            u, {"folder_id": folder_id}, None)
        assert out["files_trashed"] == 1
        assert db.session.get(StoredFile, fid).deleted_at is not None
        # missing name / folder, foreign drive, read-only drive
        assert "error" in agent_service.TOOL_HANDLERS["create_folder"](
            u, {"name": ""}, None)
        assert "error" in agent_service.TOOL_HANDLERS["create_folder"](
            u, {"name": "X", "drive_id": 999999}, None)
        assert "error" in agent_service.TOOL_HANDLERS["create_folder"](
            u, {"name": "X", "drive_id": sid}, None)
        assert "error" in agent_service.TOOL_HANDLERS["delete_folder"](
            u, {}, None)
        assert "error" in agent_service.TOOL_HANDLERS["delete_folder"](
            u, {"folder_id": 999999}, None)


def test_conversations_isolated_between_users(auth_client, app, user):
    _make_indexed_file(app, "x.txt", "hello")

    app.config["AI_ENABLED"] = True
    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "hi"}):
        resp = auth_client.post("/ai/chat", json={"message": "hello"})
        _ = resp.data  # consume the stream inside the patch

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
    assert types == ["thinking", "tool_start", "step", "tool_result", "answer"]
    assert events[0]["content"] == "Let me search for lunch info."
    assert "Found 1 file(s): menu.txt" in events[3]["result"]["summary"]

    with app.app_context():
        conv = ChatConversation.query.one()
        roles = [m.role for m in conv.messages]
        assert roles == ["user", "thinking", "step", "assistant"]


def test_tool_start_emitted_before_handler_runs(app, user):
    """Live-animation contract: tool_start arrives BEFORE the tool handler
    executes — the client animates a working chip while the call runs."""
    marker = []
    entry = agent_service._TOOL_REGISTRY["search_files"]
    real_handler = entry["handler"]

    def scripted_handler(user_obj, args, drive_):
        marker.append("ran")
        return {"results": []}

    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "search_files", "arguments": '{"query": "x"}'},
        }]},
        {"role": "assistant", "content": "Nothing here."},
    ])

    with app.app_context():
        user_obj = db.session.get(User, user)
        entry["handler"] = scripted_handler
        try:
            with patch("app.services.ai_service.chat_completion",
                       side_effect=lambda *a, **k: next(responses)):
                for kind, _payload in agent_service.run_agent_events(
                        user_obj, [{"role": "user", "content": "x?"}]):
                    if kind == "tool_start":
                        assert marker == []  # nothing has executed yet
        finally:
            entry["handler"] = real_handler

    assert marker == ["ran"]


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
        _ = resp.data  # consume the stream inside the patch

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
        _ = resp.data

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
    assert kinds == ["thinking", "tool_start", "step", "tool_result", "answer"]
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
        _ = resp.data

    convs = auth_client.get("/ai/conversations").get_json()
    assert len(convs) == 1
    assert convs[0]["model"] == "test-model-9000"
    assert convs[0]["updated_at"]


# ---------------------------------------------------------------- streaming


def _stream_script(script):
    """Fake chat_completion_stream: each call pops the next event list."""
    it = iter(script)

    def fake(*args, **kwargs):
        return iter(next(it))

    return fake


_SEARCH_LUNCH = {
    "id": "call_1", "type": "function",
    "function": {"name": "search_files", "arguments": '{"query": "lunch"}'},
}


def test_agent_streams_tokens(app, user):
    _make_indexed_file(app, "menu.txt", "Today's lunch is tomato soup.")
    app.config["AI_STREAMING"] = True

    script = [
        [("reasoning", "Looking "), ("reasoning", "for lunch."),
         ("done", {"role": "assistant", "content": "",
                   "reasoning_content": "Looking for lunch.",
                   "tool_calls": [_SEARCH_LUNCH]})],
        [("token", "Lunch "), ("token", "is soup."),
         ("done", {"role": "assistant", "content": "Lunch is soup."})],
    ]

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion_stream",
                   side_effect=_stream_script(script)):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "lunch?"}]))

    kinds = [k for k, _ in events]
    assert kinds == ["thinking_token", "thinking_token", "thinking",
                     "tool_start", "step", "tool_result",
                     "answer_token", "answer_token", "answer"]
    assert events[-1] == ("answer", "Lunch is soup.")


def test_agent_falls_back_when_streaming_fails_before_deltas(app, user):
    app.config["AI_STREAMING"] = True

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion_stream",
                   side_effect=agent_service.ai_service.AIError("no stream")), \
             patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant", "content": "hi"}) as plain:
            answer, _ = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "hello"}])

    assert answer == "hi"
    assert plain.call_count == 1


def test_agent_no_fallback_when_streaming_404(app, user):
    # A 404 means the model/endpoint does not exist on the provider — the
    # non-streaming retry would fail identically, so it must not be attempted.
    app.config["AI_STREAMING"] = True

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion_stream",
                   side_effect=agent_service.ai_service.AIError(
                       "AI backend returned 404: No matching route found.",
                       status_code=404)), \
             patch("app.services.ai_service.chat_completion") as plain:
            with pytest.raises(agent_service.ai_service.AIError) as excinfo:
                list(agent_service.run_agent_events(
                    user_obj, [{"role": "user", "content": "hello"}]))

    assert plain.call_count == 0  # no non-streaming retry on 404
    assert excinfo.value.status_code == 404


def test_agent_midstream_failure_propagates(app, user):
    app.config["AI_STREAMING"] = True

    def broken_stream(*args, **kwargs):
        yield ("token", "partial")
        raise agent_service.ai_service.AIError("connection reset")

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion_stream",
                   side_effect=broken_stream), \
             patch("app.services.ai_service.chat_completion") as plain:
            with pytest.raises(agent_service.ai_service.AIError):
                list(agent_service.run_agent_events(
                    user_obj, [{"role": "user", "content": "hello"}]))

    assert plain.call_count == 0  # no fallback once deltas were sent


def test_chat_endpoint_streaming_no_duplicate_thinking(auth_client, app, user):
    _make_indexed_file(app, "menu.txt", "Today's lunch is tomato soup.")
    app.config["AI_ENABLED"] = True
    app.config["AI_STREAMING"] = True

    script = [
        [("reasoning", "thinking aloud"),
         ("done", {"role": "assistant", "content": "",
                   "reasoning_content": "thinking aloud",
                   "tool_calls": [_SEARCH_LUNCH]})],
        [("token", "Lunch is tomato soup."),
         ("done", {"role": "assistant", "content": "Lunch is tomato soup."})],
    ]

    with patch("app.services.ai_service.chat_completion_stream",
               side_effect=_stream_script(script)):
        resp = auth_client.post("/ai/chat", json={"message": "lunch?"})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    types = [e["type"] for e in events]
    # Reasoning came as live tokens — the full thinking event is NOT
    # re-sent (it would duplicate), but it is still persisted to the DB.
    assert types == ["thinking_token", "tool_start", "step", "tool_result",
                     "answer_token", "answer"]
    assert events[0]["content"] == "thinking aloud"
    assert events[-1]["answer"] == "Lunch is tomato soup."

    with app.app_context():
        conv = ChatConversation.query.one()
        roles = [m.role for m in conv.messages]
        assert roles == ["user", "thinking", "step", "assistant"]


def test_chat_endpoint_migrates_answer_tokens_to_thinking(auth_client, app, user):
    # The model streamed content ("Let me search.") and then decided to call
    # a tool: the client must move that tentative answer bubble into the
    # reasoning box (thinking event with migrate=true).
    _make_indexed_file(app, "menu.txt", "Today's lunch is tomato soup.")
    app.config["AI_ENABLED"] = True
    app.config["AI_STREAMING"] = True

    script = [
        [("token", "Let me search."),
         ("done", {"role": "assistant", "content": "Let me search.",
                   "tool_calls": [_SEARCH_LUNCH]})],
        [("token", "Soup."),
         ("done", {"role": "assistant", "content": "Soup."})],
    ]

    with patch("app.services.ai_service.chat_completion_stream",
               side_effect=_stream_script(script)):
        resp = auth_client.post("/ai/chat", json={"message": "lunch?"})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    types = [e["type"] for e in events]
    assert types == ["answer_token", "thinking", "tool_start", "step",
                     "tool_result", "answer_token", "answer"]
    thinking = next(e for e in events if e["type"] == "thinking")
    assert thinking["content"] == "Let me search."
    assert thinking["migrate"] is True

    with app.app_context():
        conv = ChatConversation.query.one()
        roles = [m.role for m in conv.messages]
        assert roles == ["user", "thinking", "step", "assistant"]


# ------------------------------------------------------------- new tools


def test_grep_file_finds_lines_with_context(app, user):
    text = ("line one\nline two target here\nline three\nline four\n"
            "line five target again\nline six")
    file_id = _make_indexed_file(app, "grepme.txt", text)

    with app.app_context():
        from app.services.agent_service import _tool_grep_file
        user_obj = db.session.get(User, user)
        res = _tool_grep_file(user_obj, file_id, "target", context=1)

    assert res["matches"] == 2
    assert res["truncated"] is False
    assert "2: line two target here" in res["content"]
    assert "1: line one" in res["content"]          # context before
    assert "5: line five target again" in res["content"]
    assert "4: line four" in res["content"]          # context after


def test_grep_file_no_match_missing_and_empty(app, user):
    file_id = _make_indexed_file(app, "plain.txt", "nothing special")

    with app.app_context():
        from app.services.agent_service import _tool_grep_file
        user_obj = db.session.get(User, user)
        assert _tool_grep_file(user_obj, file_id, "absent")["matches"] == 0
        assert "error" in _tool_grep_file(user_obj, 9999, "x")
        assert "error" in _tool_grep_file(user_obj, file_id, "")


def test_grep_file_caps_output(app, user):
    # 30 long matching lines (~400 chars each) exceed the 8000-char budget
    # before the 40-match cap, so truncation must kick in.
    text = "\n".join(f"{'x' * 390} hit row {i}" for i in range(30))
    file_id = _make_indexed_file(app, "many.txt", text)

    with app.app_context():
        from app.services.agent_service import GREP_MAX_CHARS, _tool_grep_file
        user_obj = db.session.get(User, user)
        res = _tool_grep_file(user_obj, file_id, "hit", context=0)

    assert res["matches"] == 30
    assert res["truncated"] is True
    assert len(res["content"]) <= GREP_MAX_CHARS + 200  # separators slack


def test_agent_uses_count_and_grep_steps(app, user):
    file_id = _make_indexed_file(
        app, "policy.txt", "vacation is 25 days\nremote work allowed")

    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "count_files",
                         "arguments": '{"query": "vacation"}'},
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c2", "type": "function",
            "function": {"name": "grep_file",
                         "arguments": f'{{"file_id": {file_id}, "pattern": "vacation"}}'},
        }]},
        {"role": "assistant", "content": "You get 25 vacation days."},
    ])

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "How much vacation?"}])

    assert [s["label"] for s in steps] == ["Counted files", "Searched inside file"]
    assert answer == "You get 25 vacation days."


def test_search_files_tool_returns_results_and_total(app, user):
    _make_indexed_file(app, "alpha.txt", "shared needle body")
    _make_indexed_file(app, "beta.txt", "shared needle body")

    with app.app_context():
        user_obj = db.session.get(User, user)
        out = agent_service.TOOL_HANDLERS["search_files"](
            user_obj, {"query": "needle", "sort": "name"}, None)

    assert out["total"] == 2
    assert [r["name"] for r in out["results"]] == ["alpha.txt", "beta.txt"]
    # Compact payload: empty fields are dropped.
    assert all("word_count" in r for r in out["results"])


def test_count_files_tool_shape(app, user):
    _make_indexed_file(app, "one.txt", "shared body")
    _make_indexed_file(app, "two.md", "shared body")

    with app.app_context():
        user_obj = db.session.get(User, user)
        out = agent_service.TOOL_HANDLERS["count_files"](
            user_obj, {"query": "shared"}, None)

    assert out["total"] == 2
    assert out["by_extension"] == {"txt": 1, "md": 1}


def test_list_files_recursive(app, user):
    _make_indexed_file(app, "a.txt", "alpha")
    _make_indexed_file(app, "b.txt", "beta")

    with app.app_context():
        user_obj = db.session.get(User, user)
        tree = agent_service._tool_list_files(user_obj, recursive=True)

    assert {f["name"] for f in tree["files"]} == {"a.txt", "b.txt"}
    assert tree["truncated"] is False
    assert "folder_id" in tree["files"][0]
    assert "file_count" not in tree["files"][0]


def test_get_file_info_includes_stats(app, user):
    file_id = _make_indexed_file(app, "info.txt", "one two three")

    with app.app_context():
        user_obj = db.session.get(User, user)
        info = agent_service._tool_get_file_info(user_obj, file_id)

    assert info["word_count"] == 3
    assert info["line_count"] == 1
    assert info["folder_path"] == "/"
    assert info["has_caption"] is False
    assert isinstance(info["versions"], int)


# ---------------------------------------------------- reported-bug fixes


def test_agent_handles_list_valued_tool_results(app, user):
    # list_hashtags returns a list, not a dict — building the step detail
    # must not crash with "'list' object has no attribute 'get'".
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "list_hashtags", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": "No hashtags yet."},
    ])

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "which tags exist?"}])

    assert [s["label"] for s in steps] == ["Listed hashtags"]
    assert answer == "No hashtags yet."


def test_trim_tool_messages_keeps_recent_intact(app, user):
    from app.services.agent_service import (
        _TOOL_HISTORY_KEEP,
        _TOOL_RESULT_TRIM,
        _trim_tool_messages,
    )
    big = "x" * (_TOOL_RESULT_TRIM + 500)
    messages = [{"role": "system", "content": "sys"}]
    for i in range(_TOOL_HISTORY_KEEP + 2):
        messages.append({"role": "tool", "tool_call_id": str(i),
                         "content": f"result-{i} " + big})

    _trim_tool_messages(messages)

    tools = [m for m in messages if m["role"] == "tool"]
    # Older results are trimmed to their head + note...
    for m in tools[:-_TOOL_HISTORY_KEEP]:
        assert len(m["content"]) < _TOOL_RESULT_TRIM + 200
        assert "call the tool again" in m["content"]
    # ...while the most recent ones stay intact.
    for m in tools[-_TOOL_HISTORY_KEEP:]:
        assert m["content"].endswith(big)

    # Short results are never touched, even when old.
    short = [{"role": "tool", "tool_call_id": str(i), "content": "ok"}
             for i in range(10)]
    _trim_tool_messages(short)
    assert all(m["content"] == "ok" for m in short)


def test_chat_context_overflow_gets_friendly_error(auth_client, app, user):
    app.config["AI_ENABLED"] = True
    provider_msg = ("AI backend returned 400: This model's maximum context "
                    "length is 262144 tokens. However, you requested 0 output "
                    "tokens and your prompt contains at least 262145 input "
                    "tokens.")

    with patch("app.services.ai_service.chat_completion",
               side_effect=agent_service.ai_service.AIError(provider_msg)):
        resp = auth_client.post("/ai/chat", json={"message": "hello"})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    errors = [e for e in events if e["type"] == "error"]
    assert errors
    assert "too large for the model's context window" in errors[0]["error"]
    assert "262145" not in errors[0]["error"]


def test_chat_context_size_error_gets_friendly_message(auth_client, app, user):
    app.config["AI_ENABLED"] = True
    provider_msg = ('AI backend returned 400: {"error":"Engine protocol '
                    'predict request returned 400: request (508691 tokens) '
                    'exceeds the available context size (80640 tokens)"}')

    with patch("app.services.ai_service.chat_completion",
               side_effect=agent_service.ai_service.AIError(provider_msg)):
        resp = auth_client.post("/ai/chat", json={"message": "hello"})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    errors = [e for e in events if e["type"] == "error"]
    assert errors
    assert "too large for the model" in errors[0]["error"]
    assert "508691" not in errors[0]["error"]


# ------------------------------------------------------- context-window caps


def test_cap_tool_result_truncates_oversized_payloads(app, user):
    from app.services.agent_service import _TOOL_MSG_HARD_CAP, _cap_tool_result

    small = {"results": [{"name": "a.txt"}], "total": 1}
    assert _cap_tool_result(small) == json.dumps(small, ensure_ascii=False)

    big = {"folders": [{"name": "d" * 1000} for _ in range(100)]}
    capped = _cap_tool_result(big)
    assert len(capped) <= _TOOL_MSG_HARD_CAP + 200
    assert "truncated" in capped
    assert "more specific" in capped


def test_list_files_recursive_caps_folders(app, user):
    # Regression: a recursive list_files over a 14k-folder synced drive
    # produced a tool result larger than the whole context window.
    from app.models import Folder
    from app.services.agent_service import _tool_list_files

    with app.app_context():
        user_obj = db.session.get(User, user)
        db.session.add_all(
            Folder(name=f"folder-{i:04d}", user_id=user_obj.id)
            for i in range(600))
        db.session.commit()

        out = _tool_list_files(user_obj, recursive=True)
        assert len(out["folders"]) == 500
        assert out["folders_truncated"] is True
        assert out["truncated"] is False


def test_list_files_recursive_small_drive_not_truncated(app, user):
    from app.models import Folder
    from app.services.agent_service import _tool_list_files

    with app.app_context():
        user_obj = db.session.get(User, user)
        db.session.add_all(
            Folder(name=f"folder-{i}", user_id=user_obj.id) for i in range(3))
        db.session.commit()

        out = _tool_list_files(user_obj, recursive=True)
        assert len(out["folders"]) == 3
        assert out["folders_truncated"] is False


def test_agent_no_nudge_for_answer_with_citation_and_intent_words(app, user):
    """A final answer that cites a source is never nudged, even when it
    contains intent words like 'next' or 'I will' — nudging it made the
    model complain 'I already have the answer'."""
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
                f"Employees get 25 vacation days [handbook.txt](file://{file_id}). "
                "Next, I will also check the contract if you want."}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            answer, _ = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "vacation days?"}])

    assert "25 vacation days" in answer
    assert calls["n"] == 2  # no nudge round-trip after the cited answer


def test_agent_no_nudge_when_intent_words_are_not_at_the_end(app, user):
    """Intent is announced at the END of a message. A final answer that
    mentions 'next' early on is not mistaken for stated intent."""
    _ = _make_indexed_file(app, "handbook.txt",
                           "Vacation policy: every employee gets 25 days per year.")

    calls = {"n": 0}

    def fake_completion(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "search_files", "arguments": '{"query": "vacation"}'},
            }]}
        # "next" appears early (outside the tail window); the message ends
        # with the actual answer.
        return {"role": "assistant", "content":
                "The next section of the handbook covers sick leave. "
                + "More policy details follow here. " * 10
                + "As for your question: every employee gets 25 vacation "
                  "days per year, according to the vacation policy."}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            answer, _ = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "vacation days?"}])

    assert "25 vacation days" in answer
    assert calls["n"] == 2  # accepted as final, no nudge


def test_agent_accepts_pushback_as_final_answer(app, user):
    """After a nudge, a model that pushes back ('I already have the answer…')
    is done — nudging again produced endless 'I already have it' loops."""
    calls = {"n": 0}

    def fake_completion(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Narrates intent without a tool call -> nudge.
            return {"role": "assistant", "content": "Let me search for that."}
        # Pushback after the nudge -> must be accepted as the final answer.
        return {"role": "assistant", "content":
                "I already have the answer: 25 vacation days per year."}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "vacation days?"}]))

    kinds = [k for k, _ in events]
    assert kinds == ["thinking", "answer"]
    assert "25 vacation days" in events[-1][1]
    assert calls["n"] == 2  # the pushback was NOT nudged again


def test_agent_emits_notice_when_history_is_compacted(app, user):
    """When the prompt budget forces old messages to be dropped, the agent
    emits a visible 'notice' event (rendered as a card in the chat)."""
    app.config["AI_MAX_PROMPT_TOKENS"] = 400  # tiny budget forces drops
    history = []
    for i in range(6):
        history.append({"role": "user", "content": f"question {i} " + "x" * 200})
        history.append({"role": "assistant", "content": f"answer {i} " + "y" * 200})
    history.append({"role": "user", "content": "latest question"})

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant", "content": "ok"}):
            events = list(agent_service.run_agent_events(user_obj, history))

    kinds = [k for k, _ in events]
    assert "notice" in kinds
    notice = next(p for k, p in events if k == "notice")
    assert "compacted" in notice.lower()
    assert kinds[-1] == "answer"


def test_agent_marker_ends_answer_despite_intent_words(app, user):
    """The [[END]] marker is an explicit 'done' signal: even with intent
    words at the end, a marked answer is final and the marker is stripped."""
    calls = {"n": 0}

    def fake_completion(*args, **kwargs):
        calls["n"] += 1
        return {"role": "assistant", "content":
                "Your vacation balance is 25 days. Next, I will update the "
                "spreadsheet if you confirm.\n\n[[END]]"}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "vacation balance?"}]))

    kinds = [k for k, _ in events]
    assert kinds == ["answer"]  # no thinking/nudge round-trip
    assert calls["n"] == 1
    assert "[[END]]" not in events[-1][1]
    assert "25 days" in events[-1][1]


def test_agent_continues_when_output_is_truncated(app, user):
    """finish_reason=length means the answer was cut off by the token limit —
    never treat it as final, continue instead."""
    calls = {"n": 0}

    def fake_completion(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "finish_reason": "length",
                    "content": "The report says the vacation balance is"}
        return {"role": "assistant", "finish_reason": "stop",
                "content": "The vacation balance is 25 days."}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "vacation balance?"}]))

    kinds = [k for k, _ in events]
    assert kinds == ["thinking", "answer"]
    assert "25 days" in events[-1][1]
    assert calls["n"] == 2


def test_agent_narration_without_marker_still_nudges(app, user):
    """Fallback layer: no marker, intent in the tail -> nudge (the marker is
    a positive signal, not a hard gate for non-compliant models)."""
    calls = {"n": 0}

    def fake_completion(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": "Let me check the files."}
        return {"role": "assistant", "content": "All clear."}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=fake_completion):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "anything new?"}]))

    kinds = [k for k, _ in events]
    assert kinds == ["thinking", "answer"]  # nudge happened, then final answer
    assert calls["n"] == 2


def _scripted_run(app, user, tool_name, tool_args):
    """Run the agent with a scripted model: one tool call, then an answer.
    Returns the (kind, payload) event list."""
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": tool_name, "arguments": tool_args},
        }]},
        {"role": "assistant", "content": "Done."},
    ])
    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            return list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "go"}]))


def test_tool_events_carry_step_index_and_budget(app, user):
    """tool_start and step payloads carry the 1-based step index and the
    resolved max_steps budget, for the chat UI's step progress display."""
    file_id = _make_indexed_file(app, "menu.txt", "Today's lunch is soup.")
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "search_files", "arguments": '{"query": "lunch"}'},
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_2", "type": "function",
            "function": {"name": "read_file", "arguments": f'{{"file_id": {file_id}}}'},
        }]},
        {"role": "assistant", "content": "Soup."},
    ])

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "lunch?"}], max_steps=7))

    starts = [p for k, p in events if k == "tool_start"]
    steps = [p for k, p in events if k == "step"]
    assert [s["index"] for s in starts] == [1, 2]
    assert [s["index"] for s in steps] == [1, 2]
    assert all(s["max_steps"] == 7 for s in starts + steps)
    assert starts[0]["name"] == "search_files"


def test_tool_result_carries_file_references(app, user):
    """Tools whose results reference files expose them as files: [{id, name}]
    for the chat UI's clickable chips."""
    file_id = _make_indexed_file(app, "menu.txt", "Today's lunch is soup.")

    events = _scripted_run(app, user, "search_files", '{"query": "lunch"}')
    result = next(p for k, p in events if k == "tool_result")
    assert result["files"] == [{"id": file_id, "name": "menu.txt"}]

    events = _scripted_run(app, user, "read_file", f'{{"file_id": {file_id}}}')
    result = next(p for k, p in events if k == "tool_result")
    assert result["files"] == [{"id": file_id, "name": "menu.txt"}]

    events = _scripted_run(app, user, "get_file_info", f'{{"file_id": {file_id}}}')
    result = next(p for k, p in events if k == "tool_result")
    assert result["files"] == [{"id": file_id, "name": "menu.txt"}]


def test_tool_result_without_file_references_has_no_files_key(app, user):
    """Tools with no file references (and error results) carry no files key."""
    events = _scripted_run(app, user, "count_files", '{"query": "zzz-nothing"}')
    result = next(p for k, p in events if k == "tool_result")
    assert "files" not in result

    events = _scripted_run(app, user, "read_file", '{"file_id": 9999}')
    result = next(p for k, p in events if k == "tool_result")
    assert "files" not in result  # error result: no chips


def test_result_files_caps_and_dedupes(app, user):
    from app.services.agent_service import _RESULT_FILES_CAP, _result_files
    rows = [{"file_id": i, "name": f"f{i}.txt"} for i in range(1, 20)]
    files = _result_files({"results": rows, "total": 19})
    assert len(files) == _RESULT_FILES_CAP
    # Dedupe: same file in "file_id" and "results" appears once.
    files = _result_files({"file_id": 3, "name": "a.txt",
                           "results": [{"file_id": 3, "name": "a.txt"},
                                       {"file_id": 4, "name": "b.txt"}]})
    assert files == [{"id": 3, "name": "a.txt"}, {"id": 4, "name": "b.txt"}]
    # Not a dict / error / nothing file-shaped -> None.
    assert _result_files(["not", "a", "dict"]) is None
    assert _result_files({"error": "File not found."}) is None
    assert _result_files({"total": 0}) is None


def test_chat_endpoint_streams_step_progress_and_files(auth_client, app, user):
    """The NDJSON stream forwards index/max_steps on tool_start+step and
    files on tool_result."""
    file_id = _make_indexed_file(app, "menu.txt", "Today's lunch is tomato soup.")

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
        resp = auth_client.post("/ai/chat", json={"message": "lunch?"})
        raw = resp.data.decode()

    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    start = next(e for e in events if e["type"] == "tool_start")
    step = next(e for e in events if e["type"] == "step")
    result = next(e for e in events if e["type"] == "tool_result")
    assert start["step"]["index"] == 1 and start["step"]["max_steps"] >= 1
    assert step["step"]["index"] == 1 and step["step"]["max_steps"] >= 1
    assert result["result"]["files"] == [{"id": file_id, "name": "menu.txt"}]


# --- view_image: the AI looks at images --------------------------------------

PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
           b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _make_image_file(app, user_id, name="pic.png"):
    """Store a real 1px PNG directly (caption path needs a real file)."""
    import os
    import shutil
    import tempfile

    from app.services import file_service
    with app.app_context():
        user_obj = db.session.get(User, user_id)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(PNG_1PX)
        tmp.close()

        class FakeStorage:
            filename = name
            mimetype = "image/png"

            def save(self, path):
                shutil.copy(tmp.name, path)

        stored = file_service.save_upload(FakeStorage(), user_obj)
        fid = stored.id
        os.unlink(tmp.name)
        return fid


def test_view_image_looks_and_persists_caption(app, user):
    from app.models import StoredFile as _SF
    fid = _make_image_file(app, user)
    app.config["AI_ENABLED"] = True
    seen = {}

    def fake_caption(path, config=None, instruction=None, **kw):
        seen["instruction"] = instruction
        return "a red bicycle against a blue wall"

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.agent_service.ai_service.caption_image",
                   side_effect=fake_caption):
            result = agent_service._tool_view_image(user_obj, fid, "what color?")
    assert result["caption"] == "a red bicycle against a blue wall"
    assert "color" in seen["instruction"]
    with app.app_context():
        assert db.session.get(_SF, fid).index.caption == \
            "a red bicycle against a blue wall"


def test_view_image_rejects_non_image_missing_and_no_vision(app, user):
    file_id = _make_indexed_file(app, "notes.txt", "plain text")
    fid = _make_image_file(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert agent_service._tool_view_image(
            user_obj, file_id)["error"].startswith("'notes.txt' is not an image")
        assert agent_service._tool_view_image(
            user_obj, 999999)["error"] == "File not found."
        app.config["AI_ENABLED"] = False
        assert agent_service._tool_view_image(
            user_obj, fid)["error"] == "AI vision is not configured."


def test_agent_uses_view_image_for_visual_questions(app, user):
    fid = _make_image_file(app, user)
    responses = iter([
        {"role": "assistant", "content": "Let me look at the image.",
         "tool_calls": [{
             "id": "call_1", "type": "function",
             "function": {"name": "view_image",
                           "arguments": json.dumps({"file_id": fid})}},
         ]},
        {"role": "assistant", "content": "It shows a red bicycle. [[END]]"},
    ])
    app.config["AI_ENABLED"] = True
    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)), \
             patch("app.services.agent_service.ai_service.caption_image",
                   return_value="a red bicycle"):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user",
                            "content": "What does the picture show?"}])
    assert "red bicycle" in answer
    assert [s["label"] for s in steps] == ["Viewed image"]
    assert steps[0]["detail"] == "pic.png"


# --- Chat attachments: device upload, summary, AI brief ---------------------


def _upload(client, name, content, **kw):
    data = {"files": (io.BytesIO(content), name)}
    data.update(kw)
    return client.post("/ai/attachments", data=data,
                       content_type="multipart/form-data")


def test_ai_attachments_upload_to_current_drive(auth_client, app, user):
    resp = _upload(auth_client, "data.csv", b"a,b\n1,2\n")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["drive"]["name"] == "Personal"
    assert body["errors"] == []
    assert body["files"][0]["name"] == "data.csv"
    file_id = body["files"][0]["id"]

    # Indexed inline in tests: the summary is ready with columns + rows.
    summary = auth_client.get(f"/file/{file_id}/summary").get_json()
    assert summary["index"]["status"] == "ok"
    assert summary["summary"]["kind"] == "table"
    assert summary["summary"]["columns"] == ["a", "b"]
    assert summary["summary"]["rows"] == [["1", "2"]]


def test_ai_attachments_readonly_drive_asks_then_saves(auth_client, app, user):
    with app.app_context():
        personal = Drive(name="Personal", user_id=user)
        synced = Drive(name="RO", user_id=user, source_path="/tmp/ro-test")
        db.session.add_all([personal, synced])
        db.session.commit()
        personal_id, synced_id = personal.id, synced.id
    with auth_client.session_transaction() as sess:
        sess["drive_id"] = synced_id

    resp = _upload(auth_client, "note.txt", b"hi")
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["needs_drive_choice"] is True
    assert {d["name"] for d in body["drives"]} == {"Personal"}

    resp = _upload(auth_client, "note.txt", b"hi", drive_id=str(personal_id))
    assert resp.status_code == 200
    assert resp.get_json()["drive"] == {"id": personal_id, "name": "Personal"}

    resp = _upload(auth_client, "note.txt", b"hi", drive_id=str(synced_id))
    assert resp.status_code == 400


def test_file_summary_rejects_foreign_and_missing(auth_client, app, user):
    assert auth_client.get("/file/999999/summary").status_code == 404
    file_id = _make_indexed_file(app, "mine.txt", "secret")
    with app.app_context():
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()
    client2 = app.test_client()
    client2.post("/login", data={"username": "bob", "password": "password123"})
    assert client2.get(f"/file/{file_id}/summary").status_code == 404


def test_chat_context_includes_attachment_brief(auth_client, app, user):
    app.config["AI_ENABLED"] = True
    file_id = _upload(auth_client, "mars.txt",
                      b"hello world from mars, all systems nominal").get_json()["files"][0]["id"]

    captured = {}

    def fake(*a, **k):
        captured["messages"] = k.get("messages") or a[0]
        return {"role": "assistant", "content": "ok"}

    with patch("app.services.ai_service.chat_completion", side_effect=fake):
        resp = auth_client.post("/ai/chat", json={
            "message": "summarize", "attachments": [file_id]})
        _ = resp.data  # consume the stream inside the patch

    system_notes = [m["content"] for m in captured["messages"]
                    if m["role"] == "system"]
    assert any("hello world from mars" in note for note in system_notes)
