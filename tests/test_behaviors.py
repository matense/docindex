"""Core behaviors & personas: management page, toggles, picker filtering."""

import json

from app.extensions import db
from app.models import Behavior, ChatConversation, PersonaPref, User
from app.services import behavior_service, persona_service


def _make_behavior(user_id, name="Translator", content="Answer in English."):
    behavior = Behavior(user_id=user_id, name=name, content=content)
    db.session.add(behavior)
    db.session.commit()
    return behavior.id


# ---------------------------------------------------------------------------
# Page + CRUD
# ---------------------------------------------------------------------------

def test_behaviors_page_renders(auth_client, app, user):
    resp = auth_client.get("/behaviors/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Your behaviors" in html
    assert "Persona library" in html


def test_behaviors_crud_routes(auth_client, app, user):
    resp = auth_client.post("/behaviors/create",
                            data={"name": "Translator",
                                  "content": "Answer only in English."})
    assert resp.status_code == 302
    with app.app_context():
        behavior = Behavior.query.filter_by(name="Translator").one()
        assert behavior.user_id == user
        assert behavior.enabled is True
        bid = behavior.id
    resp = auth_client.post(f"/behaviors/{bid}/update",
                            data={"name": "Translator",
                                  "content": "Answer only in French."})
    assert resp.status_code == 302
    with app.app_context():
        assert db.session.get(Behavior, bid).content == "Answer only in French."
    resp = auth_client.post(f"/behaviors/{bid}/delete")
    assert resp.status_code == 302
    with app.app_context():
        assert db.session.get(Behavior, bid) is None


def test_behavior_routes_enforce_ownership(auth_client, app, user):
    with app.app_context():
        other = User(username="other2", email="other2@x.com")
        other.set_password("x")
        db.session.add(other)
        db.session.flush()
        fid = _make_behavior(other.id, name="F", content="foreign")
    assert auth_client.post(f"/behaviors/{fid}/update",
                            data={"name": "F", "content": "y"}
                            ).status_code == 404
    assert auth_client.post(f"/behaviors/{fid}/delete").status_code == 404
    assert auth_client.post(f"/behaviors/{fid}/toggle",
                            json={"enabled": False}).status_code == 404


# ---------------------------------------------------------------------------
# Enable/disable
# ---------------------------------------------------------------------------

def test_behavior_toggle_hides_from_presets(auth_client, app, user):
    with app.app_context():
        bid = _make_behavior(user, name="Analyst", content="Be concise.")
        _make_behavior(user, name="Poet", content="Rhyme.")
    resp = auth_client.post(f"/behaviors/{bid}/toggle", json={"enabled": False})
    assert resp.get_json() == {"ok": True, "enabled": False}

    presets = auth_client.get("/behaviors/api/presets").get_json()["behaviors"]
    assert presets == [{"label": "Poet", "value": "Rhyme."}]

    resp = auth_client.post(f"/behaviors/{bid}/toggle", json={"enabled": True})
    assert resp.get_json()["enabled"] is True
    presets = auth_client.get("/behaviors/api/presets").get_json()["behaviors"]
    assert len(presets) == 2


def test_persona_toggle_roundtrip(auth_client, app, user):
    key = persona_service.list_personas()[0]["key"]
    resp = auth_client.post(f"/behaviors/personas/{key}/toggle",
                            json={"enabled": False})
    assert resp.get_json() == {"ok": True, "enabled": False}
    with app.app_context():
        assert persona_service.is_persona_enabled(user, key) is False
        # per-user: another user still has it enabled
        assert persona_service.is_persona_enabled(999, key) is True

    # Picker endpoint excludes it; the management endpoint flags it.
    picker = auth_client.get("/ai/personas").get_json()["personas"]
    assert key not in [p["key"] for p in picker]
    allp = auth_client.get("/ai/personas?all=1").get_json()["personas"]
    entry = next(p for p in allp if p["key"] == key)
    assert entry["enabled"] is False
    assert len(allp) > 100

    resp = auth_client.post(f"/behaviors/personas/{key}/toggle",
                            json={"enabled": True})
    assert resp.get_json()["enabled"] is True
    picker = auth_client.get("/ai/personas").get_json()["personas"]
    assert key in [p["key"] for p in picker]


def test_persona_toggle_unknown_key_404(auth_client, app, user):
    resp = auth_client.post("/behaviors/personas/nope/toggle",
                            json={"enabled": False})
    assert resp.status_code == 404


def test_persona_division_toggle(auth_client, app, user):
    division = persona_service.divisions()[0]
    keys = [p["key"] for p in persona_service.list_personas()
            if p["division"] == division]
    assert len(keys) > 1

    resp = auth_client.post(
        f"/behaviors/personas/division/{division}/toggle",
        json={"enabled": False})
    assert resp.get_json() == {"ok": True, "enabled": False,
                               "count": len(keys)}
    with app.app_context():
        for key in keys:
            assert persona_service.is_persona_enabled(user, key) is False
            # per-user: another user still has them enabled
            assert persona_service.is_persona_enabled(999, key) is True
    picker = auth_client.get("/ai/personas").get_json()["personas"]
    assert not [p for p in picker if p["division"] == division]

    resp = auth_client.post(
        f"/behaviors/personas/division/{division}/toggle",
        json={"enabled": True})
    assert resp.get_json()["count"] == len(keys)
    picker = auth_client.get("/ai/personas").get_json()["personas"]
    assert len([p for p in picker if p["division"] == division]) == len(keys)

    assert auth_client.post(
        "/behaviors/personas/division/No%20Such%20Division/toggle",
        json={"enabled": False}).status_code == 404


def test_persona_detail_endpoint(auth_client, app, user):
    first = persona_service.list_personas()[0]
    resp = auth_client.get(f"/ai/personas/{first['key']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["key"] == first["key"]
    assert data["content"]
    assert auth_client.get("/ai/personas/does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# Core AI tools (moved out of the Automation module)
# ---------------------------------------------------------------------------

def test_behavior_agent_tools(app, user):
    from app.services import agent_service
    with app.app_context():
        user_obj = db.session.get(User, user)
        created = agent_service._tool_save_behavior(
            user_obj, "Poet", "Rhyme.")
        assert created["ok"]
        updated = agent_service._tool_save_behavior(
            user_obj, "Poet", "Rhyme hard.")
        assert "updated" in updated["note"] and updated["id"] == created["id"]
        listing = agent_service._tool_list_behaviors(user_obj)
        assert listing["total"] == 1
        assert listing["behaviors"][0]["content"] == "Rhyme hard."
        assert "error" in agent_service._tool_save_behavior(user_obj, "", "")

        # Disabled behaviors disappear from the tool listing.
        behavior_service.set_enabled(
            db.session.get(Behavior, created["id"]), False)
        assert agent_service._tool_list_behaviors(user_obj)["total"] == 0


def test_behavior_tools_are_core_not_module(app, user):
    """list_behaviors/save_behavior are always available (core), independent
    of the Automation module's enable flag."""
    from app.services import agent_service
    with app.app_context():
        defs, handlers, _ = agent_service._active_tools()
    names = [d["function"]["name"] for d in defs]
    assert "list_behaviors" in names
    assert "save_behavior" in names
    assert "automation.list_behaviors" not in names
