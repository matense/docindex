"""Tests for the per-connection AI rate limit (requests/minute)."""

import pytest

from app.extensions import db
from app.models import AIConnection
from app.services import ai_service


@pytest.fixture(autouse=True)
def _reset_rate_windows():
    ai_service._rate_windows.clear()
    yield
    ai_service._rate_windows.clear()


class _FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


def _cfg(rpm, key="test"):
    return {
        "enabled": True,
        "base_url": "https://api.example.com/v1",
        "api_key": "",
        "model": "m",
        "vision_model": "m",
        "timeout": 10,
        "max_steps": 16,
        "rate_limit_rpm": rpm,
        "rate_key": key,
    }


def _ok_resp():
    return _FakeResp(200, {"choices": [{"message": {"content": "hi"}}]})


def test_rate_limit_blocks_after_limit(monkeypatch, app):
    with app.app_context():
        monkeypatch.setattr(ai_service.requests, "post",
                            lambda *a, **kw: _ok_resp())
        cfg = _cfg(2)
        ai_service.chat_completion([{"role": "user", "content": "a"}], config=cfg)
        ai_service.chat_completion([{"role": "user", "content": "b"}], config=cfg)
        with pytest.raises(ai_service.AIError, match="Rate limit"):
            ai_service.chat_completion([{"role": "user", "content": "c"}], config=cfg)


def test_rate_limit_zero_is_unlimited(monkeypatch, app):
    with app.app_context():
        monkeypatch.setattr(ai_service.requests, "post",
                            lambda *a, **kw: _ok_resp())
        cfg = _cfg(0, key="unlimited")
        for i in range(40):
            ai_service.chat_completion([{"role": "user", "content": str(i)}], config=cfg)


def test_rate_limit_is_per_connection(monkeypatch, app):
    with app.app_context():
        monkeypatch.setattr(ai_service.requests, "post",
                            lambda *a, **kw: _ok_resp())
        ai_service.chat_completion([{"role": "user", "content": "a"}], config=_cfg(1, "k1"))
        # A different connection key is unaffected by k1's window.
        ai_service.chat_completion([{"role": "user", "content": "b"}], config=_cfg(1, "k2"))


def test_provider_429_gets_friendly_error(monkeypatch, app):
    with app.app_context():
        monkeypatch.setattr(ai_service.requests, "post",
                            lambda *a, **kw: _FakeResp(429, headers={"Retry-After": "12"},
                                                       text="too many requests"))
        with pytest.raises(ai_service.AIError, match="429") as excinfo:
            ai_service.chat_completion([{"role": "user", "content": "a"}], config=_cfg(0))
        assert "12" in str(excinfo.value)


def test_config_for_uses_connection_rate_limit(auth_client, app):
    auth_client.post("/settings/ai/add", data={
        "name": "Limited", "base_url": "https://api.openai.com/v1",
        "api_key": "", "model": "gpt-4o-mini", "vision_model": "",
        "rate_limit_rpm": "7", "is_active": "on",
    }, follow_redirects=True)
    with app.app_context():
        conn = AIConnection.query.one()
        assert conn.rate_limit_rpm == 7
        from app.models import User
        user = User.query.first()
        cfg = ai_service.config_for(user)
        assert cfg["rate_limit_rpm"] == 7
        assert cfg["rate_key"] == f"conn:{conn.id}"


def test_config_for_falls_back_to_default(auth_client, app):
    auth_client.post("/settings/ai/add", data={
        "name": "Default", "base_url": "https://api.openai.com/v1",
        "api_key": "", "model": "gpt-4o-mini", "vision_model": "",
        "is_active": "on",
    }, follow_redirects=True)
    with app.app_context():
        from app.models import User
        conn = AIConnection.query.one()
        assert conn.rate_limit_rpm is None
        cfg = ai_service.config_for(User.query.first())
        assert cfg["rate_limit_rpm"] == 30


def test_edit_connection_updates_rate_limit(auth_client, app):
    auth_client.post("/settings/ai/add", data={
        "name": "C", "base_url": "https://api.openai.com/v1",
        "api_key": "", "model": "gpt-4o-mini", "vision_model": "",
        "is_active": "on",
    }, follow_redirects=True)
    with app.app_context():
        cid = AIConnection.query.one().id
    auth_client.post(f"/settings/ai/{cid}/edit", data={
        "name": "C", "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini", "vision_model": "", "rate_limit_rpm": "0",
    }, follow_redirects=True)
    with app.app_context():
        assert db.session.get(AIConnection, cid).rate_limit_rpm == 0
