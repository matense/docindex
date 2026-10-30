"""AI Utils module: gating, tool handlers (valid + edge cases) and one
end-to-end agent run."""

from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import ModuleState, User
from app.services import agent_service, module_service


def _enable(app):
    with app.app_context():
        state = db.session.get(ModuleState, "aiutils")
        if state is None:
            db.session.add(ModuleState(name="aiutils", enabled=True))
        else:
            state.enabled = True
        db.session.commit()


def _handlers(app):
    with app.app_context():
        _defs, handlers, _labels = agent_service._active_tools()
        return handlers


def _call(app, name, args):
    _enable(app)  # each test gets a fresh app (and empty DB)
    handlers = _handlers(app)
    assert name in handlers, f"{name} not registered"
    return handlers[name](None, args, None)


def test_tools_hidden_until_enabled(app):
    assert "aiutils.calculate" not in _handlers(app)
    _enable(app)
    handlers = _handlers(app)
    assert "aiutils.calculate" in handlers
    assert handlers["aiutils.calculate"](None, {"expression": "2+2"}) == {
        "expression": "2+2", "result": 4}


def test_all_tools_registered_with_valid_schemas(app):
    _enable(app)
    with app.app_context():
        defs, handlers, _labels = agent_service._active_tools()
    ours = [d for d in defs
            if d["function"]["name"].startswith("aiutils.")]
    assert len(ours) == 23
    for d in ours:
        fn = d["function"]
        assert fn["parameters"]["type"] == "object"
        for req in fn["parameters"].get("required", []):
            assert req in fn["parameters"]["properties"]


def test_calculate(app):
    assert _call(app, "aiutils.calculate", {"expression": "2^10"})["result"] == 1024
    assert _call(app, "aiutils.calculate", {"expression": "200*15%"})["result"] == 30
    assert _call(app, "aiutils.calculate", {"expression": "sqrt(16) + sin(0)"})["result"] == 4
    assert "error" in _call(app, "aiutils.calculate", {"expression": "__import__('os')"})  # noqa: S102
    assert "error" in _call(app, "aiutils.calculate", {"expression": "1/0"})
    assert "error" in _call(app, "aiutils.calculate", {"expression": "2+"})
    assert "error" in _call(app, "aiutils.calculate", {"expression": "factorial(1001)"})
    assert "error" in _call(app, "aiutils.calculate", {"expression": "os.getcwd()"})
    assert "error" in _call(app, "aiutils.calculate", {"expression": "9**9**9**9**9"})


def test_statistics(app):
    out = _call(app, "aiutils.statistics", {"values": [3, 5, 7, 9]})
    assert out["results"]["mean"] == 6
    assert out["results"]["median"] == 6
    assert out["count"] == 4
    assert _call(app, "aiutils.statistics", {"values": [5], "operation": "stdev"})["stdev"] == 0.0
    assert "error" in _call(app, "aiutils.statistics", {"values": []})
    assert "error" in _call(app, "aiutils.statistics", {"values": [1, "x"]})
    assert "error" in _call(app, "aiutils.statistics", {"values": [1, float("inf")]})


def test_base_convert(app):
    assert _call(app, "aiutils.base_convert", {"value": "FF", "from_base": 16, "to_base": 10})["result"] == "255"
    assert _call(app, "aiutils.base_convert", {"value": "1010", "from_base": 2, "to_base": 16})["result"] == "A"
    assert _call(app, "aiutils.base_convert", {"value": "0xFF", "from_base": 16, "to_base": 2})["result"] == "11111111"
    assert "error" in _call(app, "aiutils.base_convert", {"value": "FF", "from_base": 10, "to_base": 16})
    assert "error" in _call(app, "aiutils.base_convert", {"value": "10", "from_base": 1, "to_base": 10})


def test_fraction(app):
    out = _call(app, "aiutils.fraction", {"value": "0.75"})
    assert out["fraction"] == "3/4" and out["decimal"] == 0.75
    out = _call(app, "aiutils.fraction", {"value": "7/3"})
    assert out["mixed"] == "2 1/3"
    assert "error" in _call(app, "aiutils.fraction", {"value": "abc"})


def test_unit_convert(app):
    assert _call(app, "aiutils.unit_convert", {"value": 5, "from_unit": "miles", "to_unit": "km"})["result"] == pytest.approx(8.04672)
    assert _call(app, "aiutils.unit_convert", {"value": 32, "from_unit": "F", "to_unit": "C"})["result"] == pytest.approx(0.0)
    assert _call(app, "aiutils.unit_convert", {"value": 1, "from_unit": "parsec", "to_unit": "ly"})["category"] == "length"
    bad = _call(app, "aiutils.unit_convert", {"value": 1, "from_unit": "furlong", "to_unit": "m"})
    assert "error" in bad and "Did you mean" not in bad["error"]  # no close match
    bad = _call(app, "aiutils.unit_convert", {"value": 1, "from_unit": "kilometerz", "to_unit": "m"})
    assert "error" in bad and "Did you mean" in bad["error"]
    assert "error" in _call(app, "aiutils.unit_convert", {"value": 1, "from_unit": "m", "to_unit": "kg"})
    assert "error" in _call(app, "aiutils.unit_convert", {"value": -1, "from_unit": "K", "to_unit": "C"})


def test_constants_and_element(app):
    keys = _call(app, "aiutils.constants", {})["constants"]
    assert "c" in keys and "G" in keys
    single = _call(app, "aiutils.constants", {"name": "alpha"})
    assert single["value"] == pytest.approx(0.0072973525693)
    multi = _call(app, "aiutils.constants", {"name": "planck"})
    assert len(multi["constants"]) == 2
    assert "error" in _call(app, "aiutils.constants", {"name": "zzz"})
    fe = _call(app, "aiutils.element", {"query": "Fe"})
    assert fe["number"] == 26 and abs(fe["atomic_mass"] - 55.845) < 1e-9
    assert _call(app, "aiutils.element", {"query": "26"})["symbol"] == "Fe"
    assert "error" in _call(app, "aiutils.element", {"query": "Unobtainium"})


def test_finance(app):
    loan = _call(app, "aiutils.loan", {"principal": 250000, "annual_rate_pct": 4, "years": 30})
    assert loan["monthly_payment"] == pytest.approx(1193.54, abs=0.01)
    assert loan["num_payments"] == 360
    assert "error" in _call(app, "aiutils.loan", {"principal": -5, "annual_rate_pct": 4, "years": 30})
    comp = _call(app, "aiutils.compound", {"principal": 10000, "annual_rate_pct": 7, "years": 20})
    assert comp["final"] == pytest.approx(40387.39, abs=0.05)
    assert "error" in _call(app, "aiutils.compound", {"principal": 100, "annual_rate_pct": 5, "years": 1, "compounds_per_year": 0})
    tip = _call(app, "aiutils.percent", {"mode": "tip", "bill": 85, "pct": 20, "people": 3})
    assert tip["per_person"] == 34.0
    assert _call(app, "aiutils.percent", {"mode": "discount", "price": 100, "pct": 25})["final_price"] == 75.0
    assert _call(app, "aiutils.percent", {"mode": "change", "old": 100, "new": 150})["pct_change"] == 50.0
    assert _call(app, "aiutils.percent", {"mode": "change", "old": 0, "new": 1})["error"]
    assert _call(app, "aiutils.percent", {"mode": "bogus"})["error"]
    fx = _call(app, "aiutils.currency_convert", {"amount": 100, "from": "USD", "to": "EUR", "rate": 0.92})
    assert fx["result"] == 92.0
    assert "error" in _call(app, "aiutils.currency_convert", {"amount": 100, "from": "US", "to": "EUR", "rate": 0.92})
    assert "error" in _call(app, "aiutils.currency_convert", {"amount": 100, "from": "USD", "to": "EUR", "rate": 0})


def test_converters(app):
    dt = _call(app, "aiutils.datetime_convert", {"value": "2026-03-15 14:30", "from_tz": "Europe/Lisbon", "to_tz": "UTC"})
    assert dt["iso"].startswith("2026-03-15T14:30") and dt["iso"].endswith("+00:00")
    assert "error" in _call(app, "aiutils.datetime_convert", {"value": "not a date"})
    assert "error" in _call(app, "aiutils.datetime_convert", {"value": "2026-01-01", "to_tz": "Mars/Olympus"})
    assert _call(app, "aiutils.roman", {"value": "2026", "direction": "to_roman"})["result"] == "MMXXVI"
    assert _call(app, "aiutils.roman", {"value": "MCMXCIV", "direction": "from_roman"})["result"] == 1994
    assert "error" in _call(app, "aiutils.roman", {"value": "0", "direction": "to_roman"})
    assert "error" in _call(app, "aiutils.roman", {"value": "ABC", "direction": "from_roman"})
    assert _call(app, "aiutils.encoding", {"text": "hi", "operation": "base64_encode"})["result"] == "aGk="
    assert "error" in _call(app, "aiutils.encoding", {"text": "!!!", "operation": "base64_decode"})
    assert _call(app, "aiutils.encoding", {"text": "a b", "operation": "url_encode"})["result"] == "a%20b"


def test_text_tools(app):
    assert _call(app, "aiutils.text_case", {"text": "hello world", "style": "camel"})["result"] == "helloWorld"
    assert _call(app, "aiutils.text_case", {"text": "helloWorld", "style": "snake"})["result"] == "hello_world"
    assert _call(app, "aiutils.text_case", {"text": "x", "style": "bogus"})["error"]
    stats = _call(app, "aiutils.text_stats", {"text": "one two three"})
    assert (stats["words"], stats["lines"]) == (3, 1)
    assert _call(app, "aiutils.text_lines", {"text": "b\na\nb", "operation": "sort"})["result"] == "a\nb\nb"
    dedup = _call(app, "aiutils.text_lines", {"text": "b\na\nb", "operation": "dedupe"})
    assert dedup["result"] == "b\na" and dedup["removed"] == 1
    found = _call(app, "aiutils.text_regex", {"text": "a1 b22", "pattern": "\\d+", "operation": "extract"})
    assert found["matches"] == ["1", "22"]
    assert "error" in _call(app, "aiutils.text_regex", {"text": "a1", "pattern": "([", "operation": "extract"})
    assert _call(app, "aiutils.text_regex", {"text": "aaa", "pattern": "a", "operation": "replace", "replacement": "b"})["result"] == "bbb"
    import hashlib as _hl
    assert _call(app, "aiutils.text_hash", {"text": "hi", "algorithm": "sha256"})["hex"] == _hl.sha256(b"hi").hexdigest()
    assert "error" in _call(app, "aiutils.text_hash", {"text": "hi", "algorithm": "rot13"})
    assert _call(app, "aiutils.text_slug", {"text": "Hello, World!"})["result"] == "hello-world"
    assert "error" in _call(app, "aiutils.text_slug", {"text": "!!!"})
    assert "error" in _call(app, "aiutils.text_stats", {"text": "x" * 100001})


def test_time_tools(app):
    now = _call(app, "aiutils.datetime_now", {})
    assert set(now) >= {"iso", "date", "time", "weekday", "iso_week", "epoch"}
    assert "error" in _call(app, "aiutils.datetime_now", {"timezone": "Nope/Nowhere"})
    assert _call(app, "aiutils.date_add", {"value": "2026-01-15", "amount": 10, "unit": "days"})["date"] == "2026-01-25"
    assert _call(app, "aiutils.date_add", {"value": "2026-01-31", "amount": 1, "unit": "months"})["date"] == "2026-02-28"
    assert _call(app, "aiutils.date_add", {"value": "2024-02-29", "amount": 1, "unit": "years"})["date"] == "2025-02-28"
    assert _call(app, "aiutils.date_add", {"value": "2026-03-15", "amount": -2, "unit": "weeks"})["date"] == "2026-03-01"
    assert _call(app, "aiutils.date_add", {"value": "today", "amount": 1, "unit": "days"})["date"]
    assert "error" in _call(app, "aiutils.date_add", {"value": "2026-06-15", "amount": 1.5, "unit": "months"})
    assert "error" in _call(app, "aiutils.date_add", {"value": "tomorrow", "amount": 1, "unit": "days"})
    assert _call(app, "aiutils.date_diff", {"start": "2026-01-01", "end": "2026-01-15"}) == {"result": 14, "unit": "days"}
    assert _call(app, "aiutils.date_diff", {"start": "2026-01-15", "end": "2026-01-01"})["result"] == 14
    assert _call(app, "aiutils.date_diff", {"start": "2026-01-15", "end": "2026-01-01", "absolute": False})["result"] == -14
    assert _call(app, "aiutils.date_diff", {"start": "2026-01-15", "end": "2026-03-14", "unit": "months"})["result"] == 1
    assert _call(app, "aiutils.date_diff", {"start": "2024-02-29", "end": "2026-02-28", "unit": "years"})["result"] == 1
    assert _call(app, "aiutils.date_diff", {"start": "2024-02-29", "end": "2026-02-28", "unit": "months"})["result"] == 23
    assert "error" in _call(app, "aiutils.date_diff", {"start": "x", "end": "2026-01-01"})
    assert "error" in _call(app, "aiutils.date_diff", {"start": "2026-01-01", "end": "2026-01-02", "unit": "eons"})


def test_agent_uses_aiutils_tool(app, user):
    _enable(app)
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "aiutils.calculate", "arguments": '{"expression": "7*6"}'},
        }]},
        {"role": "assistant", "content": "42."},
    ])
    with app.app_context():
        user_obj = db.session.get(User, user)
        with patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(responses)):
            answer, steps = agent_service.run_agent(
                user_obj, [{"role": "user", "content": "what is 7 times 6?"}])
    assert "42" in answer
    assert [s["label"] for s in steps] == ["Calculated"]


def test_reference_page_served_when_enabled(app, auth_client):
    assert auth_client.get("/m/aiutils/").status_code == 404
    _enable(app)
    resp = auth_client.get("/m/aiutils/")
    assert resp.status_code == 200
    assert b"aiutils.calculate" in resp.data


def test_profile_menu_links_aiutils_only_when_enabled(auth_client, app):
    assert b"/m/aiutils/" not in auth_client.get("/settings/profile").data
    _enable(app)
    assert b"/m/aiutils/" in auth_client.get("/settings/profile").data
    with app.app_context():
        assert "aiutils" not in [i["name"] for i in module_service.nav_items()]


def _set_toggles(app, sections=None, tools=None):
    with app.app_context():
        return module_service.set_tool_toggles(
            "aiutils", sections or [], tools or [])


def _promote_first_user(app):
    with app.app_context():
        u = User.query.first()
        u.is_admin = True
        db.session.commit()


def test_toggles_default_to_all_enabled(app):
    _enable(app)
    with app.app_context():
        assert module_service.get_tool_toggles("aiutils") == {
            "sections": [], "tools": []}
        defs, _, _ = agent_service._active_tools()
    ours = [d for d in defs
            if d["function"]["name"].startswith("aiutils.")]
    assert len(ours) == 23


def test_disable_single_tool_hides_it_from_agent(app):
    _enable(app)
    assert "aiutils.calculate" in _handlers(app)
    assert _set_toggles(app, tools=["calculate"]) == {
        "sections": [], "tools": ["calculate"]}
    handlers = _handlers(app)
    assert "aiutils.calculate" not in handlers
    assert "aiutils.statistics" in handlers  # rest of the section stays
    with app.app_context():
        defs, _, _ = agent_service._active_tools()
    assert not any(d["function"]["name"] == "aiutils.calculate"
                   for d in defs)


def test_disable_section_hides_all_its_tools(app):
    _enable(app)
    _set_toggles(app, sections=["Math"])
    handlers = _handlers(app)
    for name in ("calculate", "statistics", "base_convert", "fraction"):
        assert f"aiutils.{name}" not in handlers
    assert "aiutils.loan" in handlers  # other sections unaffected


def test_section_and_tool_toggles_combine(app):
    _enable(app)
    _set_toggles(app, sections=["Text"], tools=["loan"])
    handlers = _handlers(app)
    assert "aiutils.text_slug" not in handlers
    assert "aiutils.loan" not in handlers
    assert "aiutils.compound" in handlers


def test_unknown_toggle_names_are_ignored(app):
    _enable(app)
    saved = _set_toggles(app, sections=["Nope"], tools=["calculate", "nope"])
    assert saved == {"sections": [], "tools": ["calculate"]}
    assert "aiutils.calculate" not in _handlers(app)


def test_reenable_restores_tools(app):
    _enable(app)
    _set_toggles(app, sections=["Math"], tools=["loan"])
    assert "aiutils.calculate" not in _handlers(app)
    _set_toggles(app)
    handlers = _handlers(app)
    assert "aiutils.calculate" in handlers
    assert "aiutils.loan" in handlers


def test_toggles_post_requires_admin(app, auth_client):
    _enable(app)
    resp = auth_client.post("/m/aiutils/toggles",
                            json={"tools": ["calculate"]})
    assert resp.status_code == 403
    _promote_first_user(app)
    resp = auth_client.post("/m/aiutils/toggles",
                            json={"sections": [], "tools": ["calculate"]})
    assert resp.status_code == 200
    assert resp.get_json()["disabled"] == {
        "sections": [], "tools": ["calculate"]}
    assert "aiutils.calculate" not in _handlers(app)
    assert "aiutils.loan" in _handlers(app)


def test_reference_page_hides_disabled_from_users(app, auth_client):
    _enable(app)
    _set_toggles(app, tools=["calculate"])
    body = auth_client.get("/m/aiutils/").data
    assert b"aiutils.calculate" not in body
    assert b"aiutils.statistics" in body
    assert b"aiu-tool-toggle" not in body  # no switches for non-admins


def test_reference_page_shows_switches_to_admin(app, auth_client):
    _enable(app)
    _set_toggles(app, sections=["Math"])
    _promote_first_user(app)
    body = auth_client.get("/m/aiutils/").data
    # Disabled tools still render for admins (unchecked switches).
    assert b"aiutils.calculate" in body
    assert b"aiu-tool-toggle" in body
    assert b"aiu-section-toggle" in body
