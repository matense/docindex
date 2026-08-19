import json
from unittest.mock import patch

import pytest
import requests

from app.services import ai_service
from app.services.ai_service import AIError

CFG = {
    "enabled": True,
    "base_url": "http://ai.local/v1",
    "api_key": "",
    "model": "test-model",
    "timeout": 5,
    "streaming": True,
}


class FakeResponse:
    def __init__(self, lines, status=200, text="", headers=None):
        self._lines = lines
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.closed = False

    def iter_lines(self):
        # The real requests yields bytes; encode like a UTF-8 SSE server.
        return iter(line.encode("utf-8") if isinstance(line, str) else line
                    for line in self._lines)

    def close(self):
        self.closed = True


def _sse(delta):
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


def _collect(messages=None):
    return list(ai_service.chat_completion_stream(
        messages or [{"role": "user", "content": "hi"}], config=CFG))


def test_stream_tokens_and_done():
    lines = [_sse({"content": "Hello, "}), _sse({"content": "world"}),
             "data: [DONE]"]
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResponse(lines)):
        events = _collect()
    assert events[0] == ("token", "Hello, ")
    assert events[1] == ("token", "world")
    kind, message = events[-1]
    assert kind == "done"
    assert message["content"] == "Hello, world"
    assert "tool_calls" not in message


def test_stream_ignores_junk_and_keepalives():
    lines = ["", ": keep-alive", "data: not-json", _sse({"content": "ok"}),
             "data: [DONE]", _sse({"content": "after-done"})]
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResponse(lines)):
        events = _collect()
    assert [e for e in events if e[0] == "token"] == [("token", "ok")]


def test_stream_reasoning_and_tool_calls_assembled():
    lines = [
        _sse({"reasoning_content": "I should "}),
        _sse({"reasoning_content": "search first."}),
        # Tool call split across chunks: name first, arguments in pieces.
        _sse({"tool_calls": [{"index": 0, "id": "call_1",
                              "function": {"name": "search_files",
                                           "arguments": '{"que'}}]}),
        _sse({"tool_calls": [{"index": 0,
                              "function": {"arguments": 'ry": "x"}'}}]}),
        # A second tool call at index 1.
        _sse({"tool_calls": [{"index": 1, "id": "call_2",
                              "function": {"name": "list_files",
                                           "arguments": "{}"}}]}),
        "data: [DONE]",
    ]
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResponse(lines)):
        events = _collect()
    assert ("reasoning", "I should ") in events
    assert ("reasoning", "search first.") in events
    kind, message = events[-1]
    assert kind == "done"
    assert message["reasoning_content"] == "I should search first."
    assert [tc["id"] for tc in message["tool_calls"]] == ["call_1", "call_2"]
    assert message["tool_calls"][0]["function"]["name"] == "search_files"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"query": "x"}'
    assert message["tool_calls"][1]["function"]["name"] == "list_files"


def test_stream_rate_limit_error():
    resp = FakeResponse([], status=429, headers={"Retry-After": "12"})
    with patch("app.services.ai_service.requests.post", return_value=resp):
        with pytest.raises(AIError, match="rate limit"):
            _collect()
    assert resp.closed


def test_stream_http_error():
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResponse([], status=500, text="boom")):
        with pytest.raises(AIError, match="AI backend returned 500"):
            _collect()


def test_stream_connection_error_before_any_delta():
    with patch("app.services.ai_service.requests.post",
               side_effect=requests.ConnectionError("refused")):
        with pytest.raises(AIError, match="Cannot reach AI backend"):
            _collect()


def test_stream_drops_midway_keeps_partial_content():
    class DyingResponse(FakeResponse):
        def iter_lines(self, decode_unicode=True):
            yield _sse({"content": "partial answer"})
            raise requests.ConnectionError("reset")

    with patch("app.services.ai_service.requests.post",
               return_value=DyingResponse([])):
        events = _collect()
    kind, message = events[-1]
    assert kind == "done"
    assert message["content"] == "partial answer"


def test_stream_empty_stream_raises():
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResponse(["data: [DONE]"])):
        with pytest.raises(AIError, match="without any content"):
            _collect()


def test_stream_not_configured():
    with pytest.raises(AIError, match="not configured"):
        list(ai_service.chat_completion_stream(
            [], config={"enabled": False, "base_url": "", "model": ""}))


def test_stream_decodes_utf8_text():
    # Regression: SSE deltas without a charset header must be decoded as
    # UTF-8, not latin-1 ("análise" must not become "anÃ¡lise").
    lines = [_sse({"content": "análise está"}), _sse({"content": " formatação contém"}),
             "data: [DONE]"]
    with patch("app.services.ai_service.requests.post",
               return_value=FakeResponse(lines)):
        events = _collect()
    kind, message = events[-1]
    assert kind == "done"
    assert message["content"] == "análise está formatação contém"
