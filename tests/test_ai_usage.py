"""AI token accounting: provider usage capture, per-message badges,
global day/week/month aggregates and the profile card endpoint.

Phase 1 (this file): measured tokens in/out per message + global stats.
Phase 2 (later): cost, quotas.
"""
import json
from unittest.mock import patch

from app.extensions import db
from app.models import AITask, AIUsageEvent, ChatConversation, User
from app.services import agent_service, ai_service, ai_usage_service

CFG = {
    "enabled": True,
    "base_url": "http://ai.local/v1",
    "api_key": "",
    "model": "test-model",
    "vision_model": "test-vision",
    "timeout": 5,
    "streaming": False,
}


class FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text or json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload

    def close(self):
        pass


def _choices_msg(usage=None):
    payload = {"choices": [{"message": {"role": "assistant",
                                        "content": "hello there"},
                             "finish_reason": "stop"}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


# --- ai_service: non-streaming capture -------------------------------------


def test_chat_completion_attaches_provider_usage():
    payload = _choices_msg({"prompt_tokens": 120, "completion_tokens": 34,
                            "total_tokens": 154})
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResp(payload)):
        msg = ai_service.chat_completion(
            [{"role": "user", "content": "hi"}], config=CFG)
    assert msg["usage"] == {"prompt_tokens": 120, "completion_tokens": 34,
                            "total_tokens": 154, "estimated": False}


def test_chat_completion_estimates_missing_fields():
    payload = _choices_msg({"prompt_tokens": 50})  # no completion_tokens
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResp(payload)):
        msg = ai_service.chat_completion(
            [{"role": "user", "content": "hi"}], config=CFG)
    assert msg["usage"]["prompt_tokens"] == 50  # provider wins
    assert msg["usage"]["completion_tokens"] > 0  # estimated from content
    assert msg["usage"]["estimated"] is True


def test_chat_completion_estimates_when_usage_absent():
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResp(_choices_msg())):
        msg = ai_service.chat_completion(
            [{"role": "user", "content": "hello world, this is a test"}],
            config=CFG)
    assert msg["usage"]["prompt_tokens"] > 0
    assert msg["usage"]["estimated"] is True


# --- ai_service: streaming capture ----------------------------------------


class FakeStreamResp(FakeResp):
    def __init__(self, lines, status=200, text=""):
        super().__init__({}, status=status, text=text)
        self._lines = lines

    def iter_lines(self):
        return iter(line.encode("utf-8") for line in self._lines)


def _sse(obj):
    return "data: " + json.dumps(obj)


def _collect_stream(lines, cfg=None):
    stream_cfg = dict(CFG, streaming=True)
    with patch("app.services.ai_service.requests.post",
               return_value=FakeStreamResp(lines)) as post:
        events = list(ai_service.chat_completion_stream(
            [{"role": "user", "content": "hi"}], config=cfg or stream_cfg))
    return events, post


def test_stream_collects_usage_from_final_chunk():
    lines = [_sse({"choices": [{"delta": {"content": "hi"}}]}),
             _sse({"usage": {"prompt_tokens": 40, "completion_tokens": 5,
                             "total_tokens": 45}, "choices": []}),
             "data: [DONE]"]
    events, post = _collect_stream(lines)
    kind, message = events[-1]
    assert kind == "done"
    assert message["usage"] == {"prompt_tokens": 40, "completion_tokens": 5,
                                "total_tokens": 45, "estimated": False}
    # include_usage was requested.
    assert post.call_args[1]["json"].get("stream_options") == {"include_usage": True}


def test_stream_estimates_when_provider_silent():
    lines = [_sse({"choices": [{"delta": {"content": "hello there"}}]}),
             "data: [DONE]"]
    events, _ = _collect_stream(lines)
    _, message = events[-1]
    assert message["usage"]["prompt_tokens"] > 0
    assert message["usage"]["estimated"] is True


def test_stream_retries_without_stream_options_on_400():
    err = FakeStreamResp([], status=400, text="unknown field: stream_options")
    ok = FakeStreamResp([_sse({"choices": [{"delta": {"content": "ok"}}]}),
                         "data: [DONE]"])
    stream_cfg = dict(CFG, streaming=True)
    with patch("app.services.ai_service.requests.post",
               side_effect=[err, ok]) as post:
        events = list(ai_service.chat_completion_stream(
            [{"role": "user", "content": "hi"}], config=stream_cfg))
    assert post.call_count == 2
    assert "stream_options" not in post.call_args[1]["json"]
    _, message = events[-1]
    assert message["content"] == "ok"


# --- normalize_usage -------------------------------------------------------


def test_normalize_usage_prefers_provider_numbers():
    u = ai_usage_service.normalize_usage(
        {"prompt_tokens": 10, "completion_tokens": 3},
        [{"role": "user", "content": "x" * 400}], "y" * 400)
    assert u == {"prompt_tokens": 10, "completion_tokens": 3,
                 "total_tokens": 13, "estimated": False}


def test_normalize_usage_rejects_garbage():
    u = ai_usage_service.normalize_usage(
        {"prompt_tokens": -5, "completion_tokens": "many"},
        [{"role": "user", "content": "hello world"}], "hi")
    assert u["prompt_tokens"] > 0 and u["completion_tokens"] > 0
    assert u["estimated"] is True
    assert u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"]


# --- agent loop: usage events ----------------------------------------------


def test_run_agent_events_yields_usage_per_call(app, user):
    replies = iter([
        {"role": "assistant", "content": "final",
         "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                   "total_tokens": 120, "estimated": False}},
    ])
    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(replies)):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "hi"}]))
    kinds = [k for k, _ in events]
    assert "usage" in kinds
    usage = [p for k, p in events if k == "usage"][0]
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 20
    assert usage["estimated"] is False


def test_run_agent_events_no_usage_event_when_provider_silent(app, user):
    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant", "content": "plain [[END]]"}):
            events = list(agent_service.run_agent_events(
                user_obj, [{"role": "user", "content": "hi"}]))
    kinds = [k for k, _ in events]
    assert "usage" not in kinds  # old mocks keep old behavior
    assert kinds[-1] == "answer"


# --- task persistence: rows + per-message link -----------------------------


def _enable(app):
    app.config["AI_ENABLED"] = True


def test_task_run_records_usage_linked_to_answer(auth_client, app, user):
    _enable(app)
    reply = {"role": "assistant", "content": "done [[END]]",
             "usage": {"prompt_tokens": 200, "completion_tokens": 40,
                       "total_tokens": 240, "estimated": False}}
    with patch("app.services.ai_service.chat_completion",
               return_value=dict(reply)):
        resp = auth_client.post("/ai/tasks", json={"message": "do it"})
    assert resp.status_code == 200
    task_id = resp.get_json()["task"]["id"]
    with app.app_context():
        task = db.session.get(AITask, task_id)
        assert task.status == "done"
        rows = AIUsageEvent.query.filter_by(task_id=task_id).all()
        assert len(rows) == 1
        assert rows[0].prompt_tokens == 200
        assert rows[0].completion_tokens == 40
        assert rows[0].estimated is False
        assert rows[0].conversation_id == task.conversation_id
        answer = task.conversation.messages[-1]
        assert answer.role == "assistant"
        assert rows[0].message_id == answer.id


def test_task_run_without_usage_records_nothing(auth_client, app, user):
    _enable(app)
    with patch("app.services.ai_service.chat_completion",
               return_value={"role": "assistant", "content": "plain [[END]]"}):
        resp = auth_client.post("/ai/tasks", json={"message": "do it"})
    task_id = resp.get_json()["task"]["id"]
    with app.app_context():
        assert AIUsageEvent.query.filter_by(task_id=task_id).count() == 0


def test_conversation_includes_per_message_usage(auth_client, app, user):
    _enable(app)
    reply = {"role": "assistant", "content": "done [[END]]",
             "usage": {"prompt_tokens": 60, "completion_tokens": 12,
                       "total_tokens": 72, "estimated": True}}
    with patch("app.services.ai_service.chat_completion",
               return_value=dict(reply)):
        task_id = auth_client.post("/ai/tasks", json={"message": "do it"}).get_json()["task"]["id"]
    with app.app_context():
        conv_id = db.session.get(AITask, task_id).conversation_id
    resp = auth_client.get(f"/ai/conversations/{conv_id}")
    assert resp.status_code == 200
    assistants = [m for m in resp.get_json()["messages"]
                  if m["role"] == "assistant"]
    assert assistants[-1]["usage"] == {"prompt_tokens": 60,
                                       "completion_tokens": 12,
                                       "estimated": True}


def test_delete_conversation_removes_usage(auth_client, app, user):
    _enable(app)
    reply = {"role": "assistant", "content": "done [[END]]",
             "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                       "total_tokens": 12, "estimated": False}}
    with patch("app.services.ai_service.chat_completion",
               return_value=dict(reply)):
        task_id = auth_client.post("/ai/tasks", json={"message": "do it"}).get_json()["task"]["id"]
    with app.app_context():
        conv_id = db.session.get(AITask, task_id).conversation_id
        assert AIUsageEvent.query.filter_by(conversation_id=conv_id).count() == 1
    assert auth_client.post(f"/ai/conversations/{conv_id}/delete").status_code == 200
    with app.app_context():
        assert AIUsageEvent.query.filter_by(conversation_id=conv_id).count() == 0


# --- aggregates + profile endpoint ------------------------------------------


def _seed(app, user, days_ago, prompt, completion, kind="chat"):
    from datetime import timedelta

    from app.models import utcnow
    with app.app_context():
        row = AIUsageEvent(user_id=user, conversation_id=None, task_id=None,
                           message_id=None, kind=kind, model="m",
                           prompt_tokens=prompt,
                           completion_tokens=completion, estimated=False)
        row.created_at = utcnow() - timedelta(days=days_ago)
        db.session.add(row)
        db.session.commit()


def test_get_stats_ranges_and_series(app, user):
    _seed(app, user, 0, 100, 20)     # today
    _seed(app, user, 3, 300, 60)     # week
    _seed(app, user, 20, 1000, 200)  # month only
    _seed(app, user, 60, 5000, 1000)  # total only
    with app.app_context():
        stats = ai_usage_service.get_stats(user)
    assert stats["today"]["total_tokens"] == 120
    assert stats["week"]["total_tokens"] == 120 + 360
    assert stats["month"]["total_tokens"] == 120 + 360 + 1200
    assert stats["total"]["total_tokens"] == 120 + 360 + 1200 + 6000
    assert len(stats["daily"]) == 30
    assert stats["daily"][-1]["total_tokens"] == 120  # today last
    assert stats["week"]["calls"] == 2
    # Other users' spend never leaks in.
    with app.app_context():
        other = User(username="bob", email="bob@example.com")
        other.set_password("x")
        db.session.add(other)
        db.session.commit()
        assert ai_usage_service.get_stats(other.id)["total"]["total_tokens"] == 0


def test_profile_ai_usage_endpoint(auth_client, app, user):
    _seed(app, user, 0, 100, 20)
    resp = auth_client.get("/settings/profile/ai-usage")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["today"] == {"prompt_tokens": 100, "completion_tokens": 20,
                             "total_tokens": 120, "calls": 1}
    assert len(data["daily"]) == 30


def test_message_usage_map_sums_steps(app, user):
    with app.app_context():
        conv = ChatConversation(user_id=user, title="t")
        db.session.add(conv)
        db.session.flush()
        from app.models import ChatMessage
        msg = ChatMessage(conversation_id=conv.id, role="assistant",
                          content="a")
        db.session.add(msg)
        db.session.flush()
        for p, c in ((100, 10), (50, 5)):
            db.session.add(AIUsageEvent(
                user_id=user, conversation_id=conv.id, message_id=msg.id,
                kind="chat", prompt_tokens=p, completion_tokens=c))
        db.session.commit()
        mid, cid = msg.id, conv.id
        got = ai_usage_service.message_usage_map(cid)
    assert got[mid] == {"prompt_tokens": 150, "completion_tokens": 15,
                        "estimated": False}


def test_record_usage_never_raises(app, user):
    with app.app_context():
        assert ai_usage_service.record_usage(None, None) is None
        assert ai_usage_service.record_usage(user, "garbage") is None
        assert ai_usage_service.record_usage(user, {}) is not None


def test_format_tokens():
    f = ai_usage_service.format_tokens
    assert f(950) == "950"
    assert f(12400) == "12.4k"
    assert f(12000) == "12k"
    assert f(2000000) == "2M"
