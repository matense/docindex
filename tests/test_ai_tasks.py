"""Long-horizon background AI tasks: lifecycle, routes, isolation, recovery.

Tasks run inline here (TestConfig.AI_TASKS_ASYNC = False), so start_task()
returns with the task already finished.
"""

import json
from unittest.mock import patch

from app.extensions import db
from app.models import AITask, ChatConversation, ChatMessage, Drive, StoredFile, User
from app.services import agent_service, ai_task_service


def _enable_ai(app):
    app.config["AI_ENABLED"] = True


def _answers(*texts):
    """chat_completion side_effect: one plain answer per call."""
    responses = iter([{"role": "assistant", "content": t} for t in texts])
    return lambda *a, **k: next(responses)


def test_start_task_runs_to_done_and_persists_transcript(auth_client, app, user):
    _enable_ai(app)
    with patch("app.services.ai_service.chat_completion",
               side_effect=_answers("All drives summarized.")):
        resp = auth_client.post("/ai/tasks", json={"message": "Summarize my drives"})

    assert resp.status_code == 200
    task_id = resp.get_json()["task"]["id"]
    with app.app_context():
        task = db.session.get(AITask, task_id)
        assert task.status == "done"
        assert task.started_at and task.finished_at
        conv = db.session.get(ChatConversation, task.conversation_id)
        roles = [m.role for m in conv.messages]
        assert roles[0] == "user"
        assert roles[-1] == "assistant"
        assert conv.messages[-1].content == "All drives summarized."


def test_start_task_requires_message(auth_client, app, user):
    _enable_ai(app)
    resp = auth_client.post("/ai/tasks", json={"message": "  "})
    assert resp.status_code == 400


def test_start_task_requires_ai(auth_client, app, user):
    app.config["AI_ENABLED"] = False
    resp = auth_client.post("/ai/tasks", json={"message": "hello"})
    assert resp.status_code == 503


def test_task_steps_are_persisted_as_messages(auth_client, app, user):
    _enable_ai(app)
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "list_drives", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": "You have one drive."},
    ])
    with patch("app.services.ai_service.chat_completion",
               side_effect=lambda *a, **k: next(responses)):
        resp = auth_client.post("/ai/tasks", json={"message": "Count my drives"})

    task_id = resp.get_json()["task"]["id"]
    with app.app_context():
        conv = db.session.get(AITask, task_id).conversation
        step_msgs = [m for m in conv.messages if m.role == "step"]
        assert step_msgs and "→" in step_msgs[0].content
        assert conv.messages[-1].content == "You have one drive."


def test_task_error_is_captured(auth_client, app, user):
    _enable_ai(app)
    from app.services import ai_service

    def boom(*a, **k):
        raise ai_service.AIError("provider exploded")

    with patch("app.services.ai_service.chat_completion", side_effect=boom):
        resp = auth_client.post("/ai/tasks", json={"message": "do things"})

    task_id = resp.get_json()["task"]["id"]
    with app.app_context():
        task = db.session.get(AITask, task_id)
        assert task.status == "error"
        assert "provider exploded" in task.error
        # The transcript shows the failure too.
        assert "provider exploded" in task.conversation.messages[-1].content


def test_should_stop_breaks_the_agent_loop(auth_client, app, user):
    _enable_ai(app)
    calls = {"n": 0}

    def looping(*a, **k):
        calls["n"] += 1
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{calls['n']}", "type": "function",
            "function": {"name": "list_drives", "arguments": "{}"},
        }]}

    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion", side_effect=looping):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "loop forever"}],
                should_stop=lambda: calls["n"] >= 2))

    kinds = [k for k, _ in events]
    assert kinds[-1] == "stopped"
    assert calls["n"] == 2  # stopped before the third model call


def test_stop_route_marks_task_stopped(auth_client, app, user):
    _enable_ai(app)
    # Create a queued task without letting the worker run it.
    with app.app_context():
        user_obj = db.session.get(User, user)
        conv = ChatConversation(user_id=user_obj.id, title="t")
        db.session.add(conv)
        db.session.flush()
        task = AITask(user_id=user_obj.id, conversation_id=conv.id,
                      title="t", status="queued")
        db.session.add(task)
        db.session.commit()
        task_id = task.id

    resp = auth_client.post(f"/ai/tasks/{task_id}/stop")
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(AITask, task_id).status == "stopped"


def test_active_lists_running_and_unnotified_finished(auth_client, app, user):
    _enable_ai(app)
    with patch("app.services.ai_service.chat_completion",
               side_effect=_answers("done!")):
        auth_client.post("/ai/tasks", json={"message": "task one"})

    resp = auth_client.get("/ai/tasks/active")
    tasks = resp.get_json()["tasks"]
    assert len(tasks) == 1  # finished but not dismissed: still listed
    assert tasks[0]["status"] == "done"

    # Dismiss it — it leaves the dock.
    resp = auth_client.post(f"/ai/tasks/{tasks[0]['id']}/ack")
    assert resp.status_code == 200
    assert auth_client.get("/ai/tasks/active").get_json()["tasks"] == []


def test_ack_running_task_rejected(auth_client, app, user):
    with app.app_context():
        user_obj = db.session.get(User, user)
        conv = ChatConversation(user_id=user_obj.id, title="t")
        db.session.add(conv)
        db.session.flush()
        task = AITask(user_id=user_obj.id, conversation_id=conv.id,
                      title="t", status="running")
        db.session.add(task)
        db.session.commit()
        task_id = task.id
    assert auth_client.post(f"/ai/tasks/{task_id}/ack").status_code == 404


def test_tasks_are_isolated_per_user(auth_client, app, user):
    _enable_ai(app)
    with patch("app.services.ai_service.chat_completion",
               side_effect=_answers("hi")):
        auth_client.post("/ai/tasks", json={"message": "alice's task"})

    with app.app_context():
        task_id = AITask.query.first().id
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()

    client2 = app.test_client()
    client2.post("/login", data={"username": "bob", "password": "password123"})
    assert client2.get("/ai/tasks/active").get_json()["tasks"] == []
    assert client2.get(f"/ai/tasks/{task_id}").status_code == 404
    assert client2.post(f"/ai/tasks/{task_id}/stop").status_code == 404
    assert client2.post(f"/ai/tasks/{task_id}/ack").status_code == 404


def test_recover_interrupted_marks_stale_tasks(app, user):
    with app.app_context():
        user_obj = db.session.get(User, user)
        conv = ChatConversation(user_id=user_obj.id, title="t")
        db.session.add(conv)
        db.session.flush()
        task = AITask(user_id=user_obj.id, conversation_id=conv.id,
                      title="t", status="running")
        db.session.add(task)
        db.session.commit()
        task_id = task.id

    ai_task_service.recover_interrupted(app)

    with app.app_context():
        task = db.session.get(AITask, task_id)
        assert task.status == "interrupted"
        assert task.error
        assert task.finished_at


def test_task_uses_extended_step_budget(auth_client, app, user):
    _enable_ai(app)
    seen = {}

    def answer(*a, **k):
        return {"role": "assistant", "content": "ok"}

    real_events = agent_service.run_agent_events

    def spy(user, history, drive=None, **kwargs):
        seen["max_steps"] = kwargs.get("max_steps")
        seen["block"] = kwargs.get("block")
        seen["note"] = history[0]["content"] if history[0]["role"] == "system" else ""
        return real_events(user, history, drive=drive, **kwargs)

    with patch("app.services.ai_service.chat_completion", side_effect=answer), \
         patch("app.services.ai_task_service.agent_service.run_agent_events", spy):
        auth_client.post("/ai/tasks", json={"message": "big job"})

    assert seen["max_steps"] == app.config["AI_TASK_MAX_STEPS"]
    assert seen["block"] is True
    assert "long-horizon" in seen["note"]


# --- Live stream (/ai/tasks/<id>/stream) ------------------------------------


def _make_task(app, user, status):
    with app.app_context():
        user_obj = db.session.get(User, user)
        conv = ChatConversation(user_id=user_obj.id, title="t")
        db.session.add(conv)
        db.session.flush()
        task = AITask(user_id=user_obj.id, conversation_id=conv.id,
                      title="t", status=status)
        db.session.add(task)
        db.session.commit()
        return task.id


def test_stream_finished_task_returns_status_and_closes(auth_client, app, user):
    import json
    task_id = _make_task(app, user, "done")
    resp = auth_client.get(f"/ai/tasks/{task_id}/stream")
    events = [json.loads(line) for line in resp.data.decode().splitlines()
              if line.strip()]
    assert events == [{"type": "task_status", "status": "done"}]


def test_stream_running_task_receives_broadcast_events(auth_client, app, user):
    import json
    import threading
    task_id = _make_task(app, user, "running")

    # Simulate the worker broadcasting live events, then finishing.
    def produce():
        ai_task_service._broadcast(task_id, {"type": "step", "step": {
            "label": "Searched files", "detail": "vacation"}})
        ai_task_service._broadcast(task_id, {"type": "answer", "answer": "ok",
                                             "conversation_id": 1,
                                             "model": "m"})
        ai_task_service._broadcast(task_id,
                                   {"type": "task_status", "status": "done"})
        ai_task_service._broadcast(task_id, None)

    threading.Timer(0.2, produce).start()
    resp = auth_client.get(f"/ai/tasks/{task_id}/stream")
    events = [json.loads(line) for line in resp.data.decode().splitlines()
              if line.strip()]
    assert [e["type"] for e in events] == [
        "task_status", "step", "answer", "task_status"]
    assert events[0]["status"] == "running"


def test_worker_broadcasts_live_events(app, user):
    """A subscriber attached before the worker runs receives the agent's
    events (same NDJSON shape as /ai/chat) and the end-of-stream sentinel."""
    _enable_ai(app)
    task_id = _make_task(app, user, "queued")
    with app.app_context():
        q = ai_task_service.subscribe(task_id)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=_answers("live answer")):
            ai_task_service._task_worker(task_id, app)
        events = []
        while True:
            ev = q.get(timeout=2)
            if ev is None:
                break
            events.append(ev)
    kinds = [e["type"] for e in events]
    assert "answer" in kinds
    assert kinds[-1] == "task_status"
    assert events[-1]["status"] == "done"


def test_stream_requires_ownership(auth_client, app, user):
    task_id = _make_task(app, user, "running")
    with app.app_context():
        bob = User(username="carol", email="carol@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()
    client2 = app.test_client()
    client2.post("/login", data={"username": "carol", "password": "password123"})
    assert client2.get(f"/ai/tasks/{task_id}/stream").status_code == 404


def test_conversation_stays_background_until_dismissed(auth_client, app, user):
    """A task-owned conversation reports its task (reopen = background mode);
    after the user dismisses the task it becomes a normal chat again."""
    _enable_ai(app)
    with patch("app.services.ai_service.chat_completion",
               side_effect=_answers("done!")):
        resp = auth_client.post("/ai/tasks", json={"message": "bg work"})
    task_id = resp.get_json()["task"]["id"]
    with app.app_context():
        conv_id = db.session.get(AITask, task_id).conversation_id

    data = auth_client.get(f"/ai/conversations/{conv_id}").get_json()
    assert data["task"]["id"] == task_id
    assert data["task"]["status"] == "done"

    auth_client.post(f"/ai/tasks/{task_id}/ack")
    data = auth_client.get(f"/ai/conversations/{conv_id}").get_json()
    assert data["task"] is None


# --- Captured context (drives, files, cell) ---------------------------------


def _make_drive_with_file(app, user_id, drive_name, file_name, text):
    """Create a drive owning one indexed text file; returns (drive_id, file_id)."""
    from app.services import file_service, indexing_service

    class FakeStorage:
        filename = file_name
        mimetype = "text/plain"

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(text.encode())

    with app.app_context():
        u = db.session.get(User, user_id)
        drive = Drive(name=drive_name, user_id=user_id)
        db.session.add(drive)
        db.session.flush()
        stored = file_service.save_upload(FakeStorage(), u)
        stored.drive_id = drive.id
        db.session.commit()
        indexing_service.index_file(stored.id, app)
        return drive.id, stored.id


def test_task_captures_and_reuses_context(auth_client, app, user):
    """A background task snapshots its launch context (drives, attachments,
    open file) onto the task row, and the worker runs scoped to it."""
    _enable_ai(app)
    drive_id, file_id = _make_drive_with_file(
        app, user, "Work", "notes.txt", "hello work notes")
    _, open_id = _make_drive_with_file(
        app, user, "Work2", "open.txt", "the file open on screen")
    seen = {}
    real_events = agent_service.run_agent_events

    def spy(u, history, drive=None, **kwargs):
        seen["drive_ids"] = [d.id for d in drive] if drive else None
        seen["history"] = history
        return real_events(u, history, drive=drive, **kwargs)

    with patch("app.services.ai_service.chat_completion",
               side_effect=_answers("done!")), \
         patch("app.services.ai_task_service.agent_service.run_agent_events", spy):
        resp = auth_client.post("/ai/tasks", json={
            "message": "summarize this",
            "attachments": [file_id],
            "context_file_id": open_id,
            "scope_drive_ids": [drive_id],
        })

    assert resp.status_code == 200
    task_id = resp.get_json()["task"]["id"]
    with app.app_context():
        task = db.session.get(AITask, task_id)
        snap = json.loads(task.context)
        assert snap["drive_ids"] == [drive_id]
        assert snap["drive_names"] == ["Work"]
        assert snap["file_ids"] == [file_id]
        assert snap["file_names"] == ["notes.txt"]
        assert snap["context_file_id"] == open_id
        assert snap["context_file_name"] == "open.txt"
        assert snap["all_drives"] is False
        assert snap["notes"]  # resolved system notes travel with the snapshot
        # The user message shows the attachments.
        first = task.conversation.messages[0]
        assert first.role == "user" and "📎 notes.txt" in first.content

    # The worker ran scoped to the captured drive (as a list), with the
    # captured notes seeding its history.
    assert seen["drive_ids"] == [drive_id]
    notes = [m["content"] for m in seen["history"] if m["role"] == "system"]
    assert any("attached these files" in n for n in notes)
    assert any("open on screen" in n for n in notes)


def test_task_without_context_keeps_all_drives(auth_client, app, user):
    """scope_all_drives snapshots as all_drives=True and the worker runs
    unscoped (drive=None)."""
    _enable_ai(app)
    seen = {}
    real_events = agent_service.run_agent_events

    def spy(u, history, drive=None, **kwargs):
        seen["drive_ids"] = [d.id for d in drive] if drive else None
        return real_events(u, history, drive=drive, **kwargs)

    with patch("app.services.ai_service.chat_completion",
               side_effect=_answers("done!")), \
         patch("app.services.ai_task_service.agent_service.run_agent_events", spy):
        resp = auth_client.post("/ai/tasks", json={
            "message": "scan everything", "scope_all_drives": True})

    assert resp.status_code == 200
    assert seen["drive_ids"] is None
    with app.app_context():
        task = db.session.get(AITask, resp.get_json()["task"]["id"])
        assert json.loads(task.context)["all_drives"] is True


def test_conversation_endpoint_exposes_task_context(auth_client, app, user):
    """Reopening a task chat returns the captured context so the UI can show
    it instead of the user's current screen context."""
    _enable_ai(app)
    drive_id, _ = _make_drive_with_file(app, user, "Work", "a.txt", "aaa")
    with patch("app.services.ai_service.chat_completion",
               side_effect=_answers("done!")):
        resp = auth_client.post("/ai/tasks", json={
            "message": "bg work", "scope_drive_ids": [drive_id]})
    task_id = resp.get_json()["task"]["id"]
    with app.app_context():
        conv_id = db.session.get(AITask, task_id).conversation_id

    data = auth_client.get(f"/ai/conversations/{conv_id}").get_json()
    assert data["task"]["context"]["drive_names"] == ["Work"]

    # After dismissing the task the context is gone with it (normal chat).
    auth_client.post(f"/ai/tasks/{task_id}/ack")
    data = auth_client.get(f"/ai/conversations/{conv_id}").get_json()
    assert data["task"] is None


def test_chat_accepts_multiple_drive_scope(auth_client, app, user):
    """/ai/chat takes scope_drive_ids and the agent runs scoped to that list."""
    _enable_ai(app)
    d1, _ = _make_drive_with_file(app, user, "Alpha", "a.txt", "aaa")
    d2, _ = _make_drive_with_file(app, user, "Beta", "b.txt", "bbb")
    seen = {}
    real_events = agent_service.run_agent_events

    def spy(u, history, drive=None, **kwargs):
        seen["drive_ids"] = sorted(d.id for d in drive) if drive else None
        return real_events(u, history, drive=drive, **kwargs)

    with patch("app.services.ai_service.chat_completion",
               side_effect=_answers("hi")), \
         patch("app.routes.ai.agent_service.run_agent_events", spy):
        resp = auth_client.post("/ai/chat", json={
            "message": "hello", "scope_drive_ids": [d1, d2]})
        resp.data  # consume the stream inside the patch

    assert resp.status_code == 200
    assert seen["drive_ids"] == sorted([d1, d2])


def test_search_files_accepts_multiple_drives(app, user):
    """search_files with a list of drives searches all of them and nothing
    else."""
    from app.services import search_service
    _, f1 = _make_drive_with_file(app, user, "Alpha", "one.txt", "commonword alpha")
    _, f2 = _make_drive_with_file(app, user, "Beta", "two.txt", "commonword beta")
    d3, f3 = _make_drive_with_file(app, user, "Gamma", "three.txt", "commonword gamma")

    with app.app_context():
        u = db.session.get(User, user)
        drives = Drive.query.filter(Drive.name.in_(["Alpha", "Beta"])).all()
        ids = {r["file"].id for r in search_service.search_files("commonword", u, drive=drives)}
        assert ids == {f1, f2}
        # A single Drive still works (legacy scope).
        only = Drive.query.filter_by(name="Gamma").one()
        ids = {r["file"].id for r in search_service.search_files("commonword", u, drive=only)}
        assert ids == {f3}
        assert d3 != drives  # sanity: distinct scope
