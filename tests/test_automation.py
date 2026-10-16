"""Automation module: engine, nodes, runtime, scheduler, routes, AI tools."""

import importlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Drive, Folder, ModuleState, StoredFile, User

# Load the module package directly (it lives outside the app package).
_AUTO_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "modules", "automation")
_spec = importlib.util.spec_from_file_location(
    "docindex_module_automation", os.path.join(_AUTO_DIR, "__init__.py"),
    submodule_search_locations=[_AUTO_DIR])
_auto = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("docindex_module_automation", _auto)
_spec.loader.exec_module(_auto)

engine = importlib.import_module("docindex_module_automation.engine")
nodes = importlib.import_module("docindex_module_automation.nodes")
runtime = importlib.import_module("docindex_module_automation.runtime")
scheduler = importlib.import_module("docindex_module_automation.scheduler")
tools = importlib.import_module("docindex_module_automation.tools")
registry = importlib.import_module("docindex_module_automation.registry")
models = importlib.import_module("docindex_module_automation.models")
examples = importlib.import_module("docindex_module_automation.examples")

nodes.register_defaults()
NODE_TYPES = registry._NODE_TYPES

# The notebooks module (for the notebook.* node tests) — same loading
# pattern; import has no side effects until register() is called.
_NB_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "modules", "notebooks")
_nb_spec = importlib.util.spec_from_file_location(
    "docindex_module_notebooks", os.path.join(_NB_DIR, "__init__.py"),
    submodule_search_locations=[_NB_DIR])
_nb = importlib.util.module_from_spec(_nb_spec)
sys.modules.setdefault("docindex_module_notebooks", _nb)
_nb_spec.loader.exec_module(_nb)
nb_doc = importlib.import_module("docindex_module_notebooks.doc")


@pytest.fixture()
def enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="automation", enabled=True))
        db.session.commit()


def _ctx(app=None, user_id=None):
    user = db.session.get(User, user_id) if user_id else None
    return {"user": user, "app": app}


def _execute(graph, app=None, user_id=None):
    return engine.execute_graph(graph, _ctx(app, user_id),
                                node_types=NODE_TYPES)


def _workflow(app, user_id, graph=None, name="Test flow", **kw):
    with app.app_context():
        wf = models.AutomationWorkflow(
            user_id=user_id, name=name,
            graph=json.dumps(graph if graph is not None
                             else {"nodes": [], "edges": []}), **kw)
        db.session.add(wf)
        db.session.commit()
        return wf.id


def _make_file(app, user_id, name, text):
    from app.services import file_service

    class FakeStorage:
        filename = name
        mimetype = "text/plain"

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(text.encode())

    with app.app_context():
        user = db.session.get(User, user_id)
        stored = file_service.save_upload(FakeStorage(), user)
        return stored.id


def _drive(app, user_id, synced=False, name="Personal"):
    with app.app_context():
        d = Drive(name=name, user_id=user_id,
                  source_path="/tmp/real" if synced else None)
        db.session.add(d)
        db.session.commit()
        return d.id


def _text_graph(content="hello", fmt="text"):
    return {"nodes": [
        {"id": "n1", "type": "input.text", "x": 0, "y": 0,
         "config": {"content": content, "format": fmt}},
        {"id": "n2", "type": "output.result", "x": 0, "y": 0, "config": {}},
    ], "edges": [
        {"from": "n1", "from_port": "out", "to": "n2", "to_port": "in"}]}


# ---------------------------------------------------------------------------
# Engine: validation and ordering
# ---------------------------------------------------------------------------

def test_validate_detects_cycles():
    graph = {"nodes": [
        {"id": "a", "type": "transform.template", "config": {}},
        {"id": "b", "type": "transform.template", "config": {}},
    ], "edges": [
        {"from": "a", "from_port": "out", "to": "b", "to_port": "in"},
        {"from": "b", "from_port": "out", "to": "a", "to_port": "in"}]}
    with pytest.raises(engine.WorkflowError, match="cycle"):
        engine.validate_graph(graph, NODE_TYPES)


def test_validate_rejects_unknown_type():
    graph = {"nodes": [{"id": "a", "type": "bogus.node", "config": {}}],
             "edges": []}
    with pytest.raises(engine.WorkflowError, match="Unknown node type"):
        engine.validate_graph(graph, NODE_TYPES)


def test_validate_rejects_bad_ports():
    graph = {"nodes": [
        {"id": "a", "type": "input.text", "config": {}},
        {"id": "b", "type": "output.result", "config": {}},
    ], "edges": [
        {"from": "a", "from_port": "nope", "to": "b", "to_port": "in"}]}
    with pytest.raises(engine.WorkflowError, match="no output port"):
        engine.validate_graph(graph, NODE_TYPES)


def test_template_references_create_implicit_order():
    # No edge between n1 and n2 — the {{ n1 }} reference must order them.
    graph = {"nodes": [
        {"id": "n2", "type": "transform.template",
         "config": {"template": "value={{ n1 }}"}},
        {"id": "n1", "type": "input.text",
         "config": {"content": "42", "format": "text"}},
    ], "edges": []}
    _, _, order = engine.validate_graph(graph, NODE_TYPES)
    assert order.index("n1") < order.index("n2")
    outputs, _, _ = _execute(graph)
    assert outputs["n2"]["out"]["data"] == "value=42"


def test_render_template_digs_into_json():
    outputs = {"n1": {"out": engine.p_json({"items": [{"name": "Ada"}]})}}
    assert engine.render_template("{{ n1.items.0.name }}", outputs) == "Ada"
    with pytest.raises(engine.WorkflowError, match="no output"):
        engine.render_template("{{ missing }}", outputs)
    with pytest.raises(engine.WorkflowError, match="does not exist"):
        engine.render_template("{{ n1.items.9 }}", outputs)


# ---------------------------------------------------------------------------
# Engine: payload coercions
# ---------------------------------------------------------------------------

def test_payload_coercions():
    table = {"columns": ["a", "b"], "rows": [["1", "2"]]}
    assert "a,b" in engine.as_text(engine.p_table(["a", "b"], [["1", "2"]]))
    assert engine.as_table(engine.p_text("a,b\n1,2\n"))["rows"] == [["1", "2"]]
    assert engine.as_json(engine.p_text('{"x": 1}')) == {"x": 1}
    with pytest.raises(engine.WorkflowError):
        engine.as_json(engine.p_text("not json"))
    with pytest.raises(engine.WorkflowError):
        engine.as_table(engine.p_json({}))
    assert engine.as_text(engine.p_files([{"name": "f.txt"}])) == "f.txt"
    assert engine.table_to_csv(table) == engine.as_text(
        engine.p_table(["a", "b"], [["1", "2"]]))


# ---------------------------------------------------------------------------
# Execution semantics: condition branching, merge, stop
# ---------------------------------------------------------------------------

def _condition_graph(left="yes", right="yes", op="equals"):
    return {"nodes": [
        {"id": "src", "type": "input.text",
         "config": {"content": "payload", "format": "text"}},
        {"id": "cond", "type": "logic.condition",
         "config": {"left": left, "op": op, "right": right}},
        {"id": "yes", "type": "output.result", "config": {}},
        {"id": "no", "type": "output.result", "config": {}},
    ], "edges": [
        {"from": "src", "from_port": "out", "to": "cond", "to_port": "in"},
        {"from": "cond", "from_port": "true", "to": "yes", "to_port": "in"},
        {"from": "cond", "from_port": "false", "to": "no", "to_port": "in"},
    ]}


def test_condition_routes_true_branch_and_skips_false():
    outputs, results, _ = _execute(_condition_graph())
    assert outputs["cond"]["true"]["data"] == "payload"
    assert outputs["cond"]["false"] is None
    # The skipped branch produced no result.
    assert [r["node"] for r in results] == ["yes"]
    assert outputs["no"]["out"] is None


def test_condition_routes_false_branch():
    outputs, results, _ = _execute(_condition_graph(right="no"))
    assert outputs["cond"]["true"] is None
    assert outputs["cond"]["false"]["data"] == "payload"
    assert [r["node"] for r in results] == ["no"]


def test_merge_picks_arriving_branch():
    graph = _condition_graph()
    graph["nodes"].append({"id": "join", "type": "logic.merge", "config": {}})
    graph["nodes"].append({"id": "end", "type": "output.result",
                           "config": {}})
    graph["edges"] += [
        {"from": "cond", "from_port": "true", "to": "join", "to_port": "in1"},
        {"from": "cond", "from_port": "false", "to": "join", "to_port": "in2"},
        {"from": "join", "from_port": "out", "to": "end", "to_port": "in"}]
    _, results, _ = _execute(graph)
    assert any(r["node"] == "end" and r["preview"] == "payload"
               for r in results)


def test_should_stop_aborts_between_nodes():
    graph = _text_graph()
    with pytest.raises(engine._Stopped):
        engine.execute_graph(graph, _ctx(), node_types=NODE_TYPES,
                             should_stop=lambda: True)


# ---------------------------------------------------------------------------
# Nodes: inputs and transforms
# ---------------------------------------------------------------------------

def test_input_text_json_format():
    graph = _text_graph('{"a": 1}', fmt="json")
    outputs, results, _ = _execute(graph)
    assert outputs["n1"]["out"] == {"kind": "json", "data": {"a": 1}}
    assert results[0]["kind"] == "json"


def test_input_text_rejects_bad_json():
    with pytest.raises(engine.WorkflowError, match="not valid JSON"):
        _execute(_text_graph("{oops", fmt="json"))


def test_input_file_csv_becomes_table(app, user):
    fid = _make_file(app, user, "data.csv", "name,age\nAda,36\nAlan,41\n")
    with app.app_context():
        graph = {"nodes": [
            {"id": "f", "type": "input.file", "config": {"file_id": str(fid)}},
            {"id": "flt", "type": "transform.table_filter",
             "config": {"column": "age", "op": "gt", "value": "40"}},
            {"id": "sel", "type": "transform.table_select",
             "config": {"columns": "name as Person"}},
            {"id": "out", "type": "output.result", "config": {}},
        ], "edges": [
            {"from": "f", "from_port": "out", "to": "flt", "to_port": "in"},
            {"from": "flt", "from_port": "out", "to": "sel", "to_port": "in"},
            {"from": "sel", "from_port": "out", "to": "out", "to_port": "in"}]}
        outputs, results, _ = _execute(graph, app, user)
        assert outputs["flt"]["out"]["data"]["rows"] == [["Alan", "41"]]
        preview = results[0]["preview"]
        assert "Person" in preview and "Alan" in preview


def test_input_file_plain_text(app, user):
    fid = _make_file(app, user, "note.txt", "hello drive")
    with app.app_context():
        outputs, _, _ = _execute(
            {"nodes": [{"id": "f", "type": "input.file",
                        "config": {"file_id": fid}}], "edges": []},
            app, user)
        assert outputs["f"]["out"]["data"] == "hello drive"


def test_input_file_requires_existing_file(app, user):
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="does not exist"):
            _execute({"nodes": [{"id": "f", "type": "input.file",
                                 "config": {"file_id": 9999}}], "edges": []},
                     app, user)


def test_json_path_into_nested_structures():
    graph = {"nodes": [
        {"id": "n1", "type": "input.text",
         "config": {"content": '{"data": {"items": [{"name": "x"}]}}',
                    "format": "json"}},
        {"id": "n2", "type": "transform.json_path",
         "config": {"path": "data.items.0"}},
    ], "edges": [
        {"from": "n1", "from_port": "out", "to": "n2", "to_port": "in"}]}
    outputs, _, _ = _execute(graph)
    assert outputs["n2"]["out"]["data"] == {"name": "x"}

    bad = {"nodes": [
        {"id": "n1", "type": "input.text",
         "config": {"content": "{}", "format": "json"}},
        {"id": "n2", "type": "transform.json_path",
         "config": {"path": "nowhere"}},
    ], "edges": [
        {"from": "n1", "from_port": "out", "to": "n2", "to_port": "in"}]}
    with pytest.raises(engine.WorkflowError, match="does not exist"):
        _execute(bad)


def test_search_files_node(app, user):
    from app.services import indexing_service
    fid = _make_file(app, user, "handbook.txt", "vacation policy details")
    with app.app_context():
        indexing_service.index_file(fid, app)
        outputs, _, _ = _execute(
            {"nodes": [{"id": "s", "type": "docindex.search_files",
                        "config": {"query": "vacation", "limit": 5}}],
             "edges": []}, app, user)
        files = outputs["s"]["out"]["data"]
        assert any(f["file_id"] == fid for f in files)


# ---------------------------------------------------------------------------
# Nodes: AI (mocked provider)
# ---------------------------------------------------------------------------

_FAKE_CFG = {"enabled": True, "base_url": "http://fake/v1", "model": "m",
             "api_key": "", "timeout": 5}


def _ai_graph(prompt, node_type="ai.prompt", extra=None):
    return {"nodes": [
        {"id": "n1", "type": "input.text",
         "config": {"content": "context text", "format": "text"}},
        {"id": "ai", "type": node_type,
         "config": dict({"prompt": prompt}, **(extra or {}))},
        {"id": "out", "type": "output.result", "config": {}},
    ], "edges": [
        {"from": "n1", "from_port": "out", "to": "ai", "to_port": "in"},
        {"from": "ai", "from_port": "out", "to": "out", "to_port": "in"}]}


def test_ai_prompt_node(app, user):
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant",
                                 "content": "AI says hi"}) as call:
            _, results, _ = _execute(
                _ai_graph("Summarize: {{ n1 }}"), app, user)
        assert results[0]["preview"] == "AI says hi"
        sent = call.call_args[0][0]
        # n1 is both templated in AND connected — template renders inline,
        # the connected payload is appended as an Input block.
        assert sent[0]["content"] == ("Summarize: context text"
                                      "\n\nInput:\ncontext text")


def test_ai_prompt_includes_connected_input(app, user):
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant",
                                 "content": "done"}) as call:
            _execute(_ai_graph("Summarize this file."), app, user)
        sent = call.call_args[0][0]
        assert sent[0]["content"] == ("Summarize this file."
                                      "\n\nInput:\ncontext text")


def test_ai_prompt_without_input_unchanged(app, user):
    graph = {"nodes": [
        {"id": "ai", "type": "ai.prompt", "config": {"prompt": "just this"}},
        {"id": "out", "type": "output.result", "config": {}},
    ], "edges": [
        {"from": "ai", "from_port": "out", "to": "out", "to_port": "in"}]}
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant",
                                 "content": "ok"}) as call:
            _execute(graph, app, user)
        assert call.call_args[0][0][0]["content"] == "just this"


def test_ai_extract_json_includes_connected_input(app, user):
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant",
                                 "content": '{"a": 1}'}) as call:
            _execute(_ai_graph("extract the count",
                               node_type="ai.extract_json"), app, user)
        sent = call.call_args[0][0]
        assert "extract the count\n\nInput:\ncontext text" in sent[0]["content"]


def test_ai_extract_json_retries_once(app, user):
    replies = iter([
        {"role": "assistant", "content": "sure! here you go"},
        {"role": "assistant", "content": '```json\n{"total": 3}\n```'},
    ])
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.ai_service.chat_completion",
                   side_effect=lambda *a, **k: next(replies)):
            outputs, _, _ = _execute(
                _ai_graph("extract", node_type="ai.extract_json"), app, user)
        assert outputs["ai"]["out"]["data"] == {"total": 3}


def test_ai_extract_json_fails_after_two_bad_replies(app, user):
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant", "content": "nope"}):
            with pytest.raises(engine.WorkflowError,
                               match="did not return valid JSON"):
                _execute(_ai_graph("extract", node_type="ai.extract_json"),
                         app, user)


def test_ai_nodes_fail_without_configuration(app, user):
    from app.services import ai_service
    with app.app_context():
        with patch.object(ai_service, "config_for",
                          return_value={"enabled": False, "base_url": "",
                                        "model": ""}):
            with pytest.raises(engine.WorkflowError,
                               match="AI is not configured"):
                _execute(_ai_graph("hi"), app, user)


# ---------------------------------------------------------------------------
# Nodes: output.save_file
# ---------------------------------------------------------------------------

def _save_graph(drive_id, name="out.txt", folder=""):
    return {"nodes": [
        {"id": "n1", "type": "input.text",
         "config": {"content": "generated content", "format": "text"}},
        {"id": "save", "type": "output.save_file",
         "config": {"name": name, "drive_id": str(drive_id),
                    "folder": folder}},
    ], "edges": [
        {"from": "n1", "from_port": "out", "to": "save", "to_port": "in"}]}


def test_save_file_creates_editable_file(app, user):
    drive_id = _drive(app, user)
    with app.app_context():
        _, _, created = _execute(_save_graph(drive_id), app, user)
        assert len(created) == 1
        stored = db.session.get(StoredFile, created[0]["file_id"])
        assert stored.name == "out.txt"
        assert stored.drive_id == drive_id
        from app.services import file_service
        assert file_service.read_text_content(stored) == "generated content"


def test_save_file_into_existing_folder(app, user):
    drive_id = _drive(app, user)
    with app.app_context():
        drive = db.session.get(Drive, drive_id)
        db.session.add(Folder(drive_id=drive.id, name="Reports",
                              user_id=user))
        db.session.commit()
        _, _, created = _execute(_save_graph(drive_id, folder="Reports"),
                                 app, user)
        stored = db.session.get(StoredFile, created[0]["file_id"])
        assert stored.folder.name == "Reports"


def test_save_file_refuses_synced_drive(app, user):
    drive_id = _drive(app, user, synced=True)
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="read-only"):
            _execute(_save_graph(drive_id), app, user)


def test_save_file_refuses_missing_folder(app, user):
    drive_id = _drive(app, user)
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="does not exist"):
            _execute(_save_graph(drive_id, folder="Nope"), app, user)


# ---------------------------------------------------------------------------
# Runtime: lifecycle, stop, recovery
# ---------------------------------------------------------------------------

def test_start_run_sync_completes(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("hello world"))
    with app.app_context():
        wf = db.session.get(models.AutomationWorkflow, wf_id)
        run = runtime.start_run(wf, db.session.get(User, user), app=app)
        assert run.status == "done"
        entries = json.loads(run.log)
        assert [e["node"] for e in entries] == ["n1", "n2"]
        assert all(e["status"] == "done" for e in entries)
        output = json.loads(run.output)
        assert output["results"][0]["preview"] == "hello world"
        wf = db.session.get(models.AutomationWorkflow, wf_id)
        assert wf.last_run_at is not None


def test_start_run_records_node_errors(app, user, enabled):
    graph = {"nodes": [{"id": "n1", "type": "input.text",
                        "config": {"content": "{bad", "format": "json"}}],
             "edges": []}
    wf_id = _workflow(app, user, graph)
    with app.app_context():
        wf = db.session.get(models.AutomationWorkflow, wf_id)
        run = runtime.start_run(wf, db.session.get(User, user), app=app)
        assert run.status == "error"
        assert "not valid JSON" in (run.error or "")


def test_engine_emits_running_before_final_entry(app, user):
    """Each node logs a transient 'running' entry before its final one."""
    events = []
    with app.app_context():
        engine.execute_graph(_text_graph(), _ctx(app, user),
                             node_types=NODE_TYPES,
                             log=lambda *a: events.append(a))
    seq = [(nid, status) for nid, _label, status, _s, _ms in events]
    assert seq == [("n1", "running"), ("n1", "done"),
                   ("n2", "running"), ("n2", "done")]


def test_run_status_reports_progress_and_pipeline(auth_client, app, user,
                                                  enabled):
    wf_id = _workflow(app, user, _text_graph("progress check"))
    resp = auth_client.post(f"/m/automation/{wf_id}/run")
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]
    data = auth_client.get(f"/m/automation/runs/{run_id}/status").get_json()
    assert data["status"] == "done"
    # Synchronous run: everything finished, no transient entries left.
    assert data["progress"]["percent"] == 100
    assert data["progress"]["total"] == 2
    assert data["progress"]["finished"] == 2
    assert data["progress"]["current"] is None
    assert [n["node"] for n in data["pipeline"]] == ["n1", "n2"]
    assert all(e["status"] != "running" for e in data["log"])


def test_run_captures_node_outputs(auth_client, app, user, enabled):
    """Each run stores per-node output previews for the popup."""
    wf_id = _workflow(app, user, _text_graph("captured output"))
    resp = auth_client.post(f"/m/automation/{wf_id}/run")
    assert resp.status_code == 200
    data = auth_client.get(
        f"/m/automation/runs/{resp.get_json()['run_id']}/status").get_json()
    assert data["node_outputs"]["n1"]["out"] == "captured output"
    assert data["node_outputs"]["n2"]["out"] == "captured output"


def test_run_status_lists_input_files(auth_client, app, user, enabled):
    """The inputs section references the trigger/static files used."""
    file_id = _make_file(app, user, "input-data.txt", "some data")
    graph = {"nodes": [
        {"id": "n1", "type": "input.file", "x": 0, "y": 0,
         "config": {"file_id": file_id}},
        {"id": "n2", "type": "output.result", "x": 0, "y": 0, "config": {}},
    ], "edges": [{"from": "n1", "from_port": "out",
                  "to": "n2", "to_port": "in"}]}
    wf_id = _workflow(app, user, graph)
    resp = auth_client.post(f"/m/automation/{wf_id}/run")
    assert resp.status_code == 200
    data = auth_client.get(
        f"/m/automation/runs/{resp.get_json()['run_id']}/status").get_json()
    assert data["status"] == "done"
    assert data["inputs"] == [
        {"file_id": file_id, "name": "input-data.txt", "via": "node n1"}]


def test_format_duration():
    from app import format_duration
    assert format_duration(None) == ""
    assert format_duration(0.42) == "420 ms"
    assert format_duration(3.25) == "3.25s"
    assert format_duration(12) == "12s"
    assert format_duration(65.34) == "1m 05.34s"
    assert format_duration(3723) == "1h 02m 03s"


def test_stop_marks_queued_run(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph())
    with app.app_context():
        wf = db.session.get(models.AutomationWorkflow, wf_id)
        run = models.AutomationRun(workflow_id=wf.id, user_id=user,
                                   status="queued", trigger="manual")
        db.session.add(run)
        db.session.commit()
        stopped = runtime.stop_run(db.session.get(User, user), run.id)
        assert stopped.status == "stopped"
        assert not runtime.has_active_run(wf_id)


def test_recover_interrupted_marks_stale_runs(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph())
    with app.app_context():
        run = models.AutomationRun(workflow_id=wf_id, user_id=user,
                                   status="running", trigger="schedule")
        db.session.add(run)
        db.session.commit()
        runtime.recover_interrupted(app)
        run = db.session.get(models.AutomationRun, run.id)
        assert run.status == "interrupted"


def test_runs_are_isolated_per_user(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph())
    with app.app_context():
        other = User(username="bob", email="bob@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        assert runtime.stop_run(other, 1) is None  # not his run
        assert runtime.get_run(other, 1) is None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def test_due_workflows_rules(app, user):
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    with app.app_context():
        never_ran = _workflow(app, user, _text_graph(),
                              enabled=True, interval_minutes=10)
        recent = _workflow(app, user, _text_graph(), name="recent",
                           enabled=True, interval_minutes=10,
                           last_run_at=now - timedelta(minutes=5))
        stale = _workflow(app, user, _text_graph(), name="stale",
                          enabled=True, interval_minutes=10,
                          last_run_at=now - timedelta(minutes=11))
        _workflow(app, user, _text_graph(), name="disabled",
                  enabled=False, interval_minutes=10)
        _workflow(app, user, _text_graph(), name="no interval",
                  enabled=True, interval_minutes=None)
        due = {wf.id for wf in scheduler.due_workflows(now)}
        assert never_ran in due and stale in due
        assert recent not in due


def test_due_skips_workflows_with_active_run(app, user):
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    with app.app_context():
        wf_id = _workflow(app, user, _text_graph(),
                          enabled=True, interval_minutes=10)
        db.session.add(models.AutomationRun(
            workflow_id=wf_id, user_id=user, status="running"))
        db.session.commit()
        assert scheduler.due_workflows(now) == []


def test_tick_launches_due_runs_when_enabled(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("tick"),
                      enabled=True, interval_minutes=10)
    assert scheduler.tick(app) == 1
    with app.app_context():
        run = (models.AutomationRun.query
               .filter_by(workflow_id=wf_id).one())
        assert run.status == "done"
        assert run.trigger == "schedule"


def test_tick_noop_when_module_disabled(app, user):
    _workflow(app, user, _text_graph(), enabled=True, interval_minutes=10)
    assert scheduler.tick(app) == 0


# ---------------------------------------------------------------------------
# AI tools
# ---------------------------------------------------------------------------

def test_tools_list_and_run(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("via tool"), name="My flow")
    with app.app_context():
        user_obj = db.session.get(User, user)
        listing = tools._tool_list(user_obj, {}, None)
        assert listing["total"] == 1
        assert listing["workflows"][0]["name"] == "My flow"
        result = tools._tool_run(user_obj, {"workflow_id": wf_id}, None)
        assert result["status"] == "done"
        run = db.session.get(models.AutomationRun, result["run_id"])
        assert run.trigger == "ai"


def test_tool_run_rejects_unknown_workflow(app, user, enabled):
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert "error" in tools._tool_run(user_obj, {"workflow_id": 999}, None)
        assert "error" in tools._tool_run(user_obj, {"workflow_id": "x"}, None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_module_routes_404_when_disabled(auth_client):
    assert auth_client.get("/m/automation/").status_code == 404


def test_list_and_create_flow(auth_client, app, user, enabled):
    resp = auth_client.get("/m/automation/")
    assert resp.status_code == 200
    resp = auth_client.post("/m/automation/create",
                            data={"name": "Daily report"})
    assert resp.status_code == 302
    with app.app_context():
        wf = models.AutomationWorkflow.query.filter_by(
            name="Daily report").one()
        assert wf.user_id == user


def test_index_shows_sections_and_recent_runs(auth_client, app, user,
                                              enabled):
    wf_id = _workflow(app, user, _text_graph("index page"))
    resp = auth_client.post(f"/m/automation/{wf_id}/run")
    assert resp.status_code == 200
    resp = auth_client.get("/m/automation/")
    assert resp.status_code == 200
    html = resp.data.decode()
    # Collapsible examples gallery, workflows section and recent runs.
    assert 'id="automation-examples"' in html
    assert "Your workflows" in html
    assert "Recent runs" in html
    assert "Test flow" in html  # workflow name in the recent runs list


def test_editor_page_renders(auth_client, app, user, enabled):
    _drive(app, user)  # exercises the drives serialization on the page
    wf_id = _workflow(app, user, _text_graph())
    resp = auth_client.get(f"/m/automation/{wf_id}")
    assert resp.status_code == 200
    assert b"input.text" in resp.data  # palette rendered
    assert b"Personal" in resp.data  # drive list serialized


def test_save_validates_graph(auth_client, app, user, enabled):
    wf_id = _workflow(app, user)
    resp = auth_client.post(f"/m/automation/{wf_id}/save",
                            json={"graph": _text_graph("hi")})
    assert resp.get_json() == {"ok": True}
    resp = auth_client.post(
        f"/m/automation/{wf_id}/save",
        json={"graph": {"nodes": [{"id": "x", "type": "bogus.node"}],
                        "edges": []}})
    assert resp.status_code == 400
    assert "Unknown node type" in resp.get_json()["error"]


def test_save_schedule_fields(auth_client, app, user, enabled):
    wf_id = _workflow(app, user)
    resp = auth_client.post(f"/m/automation/{wf_id}/save",
                            json={"graph": _text_graph(), "enabled": True,
                                  "interval_minutes": 15})
    assert resp.get_json() == {"ok": True}
    with app.app_context():
        wf = db.session.get(models.AutomationWorkflow, wf_id)
        assert wf.enabled and wf.interval_minutes == 15
    resp = auth_client.post(f"/m/automation/{wf_id}/save",
                            json={"graph": _text_graph(),
                                  "interval_minutes": -5})
    assert resp.status_code == 400


def test_run_endpoint_executes_and_reports(auth_client, app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("endpoint run"))
    resp = auth_client.post(f"/m/automation/{wf_id}/run")
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]
    status = auth_client.get(f"/m/automation/runs/{run_id}/status")
    assert status.get_json()["status"] == "done"
    assert auth_client.get(f"/m/automation/runs/{run_id}").status_code == 200
    assert auth_client.get(f"/m/automation/{wf_id}/runs").status_code == 200


def test_run_endpoint_rejects_empty_workflow(auth_client, app, user, enabled):
    wf_id = _workflow(app, user)  # empty graph
    resp = auth_client.post(f"/m/automation/{wf_id}/run")
    assert resp.status_code == 400


def test_duplicate_copies_graph_but_not_schedule(auth_client, app, user,
                                                 enabled):
    wf_id = _workflow(app, user, _text_graph("dup"), enabled=True,
                      interval_minutes=30)
    resp = auth_client.post(f"/m/automation/{wf_id}/duplicate")
    assert resp.status_code == 302
    with app.app_context():
        clone = (models.AutomationWorkflow.query
                 .filter_by(name="Test flow (copy)").one())
        assert clone.enabled is False
        assert clone.interval_minutes == 30
        assert json.loads(clone.graph)["nodes"]


def test_workflow_pages_require_ownership(auth_client, app, user, enabled):
    with app.app_context():
        other = User(username="carol", email="carol@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        wf = models.AutomationWorkflow(user_id=other.id, name="Not yours",
                                       graph="")
        db.session.add(wf)
        db.session.commit()
        wf_id = wf.id
    assert auth_client.get(f"/m/automation/{wf_id}").status_code == 404
    assert auth_client.post(f"/m/automation/{wf_id}/run").status_code == 404


# ---------------------------------------------------------------------------
# File browser endpoint
# ---------------------------------------------------------------------------

def _stored(app, user_id, drive_id, name, folder_id=None, deleted=False):
    with app.app_context():
        f = StoredFile(name=name, stored_name="blob_" + name,
                       user_id=user_id, drive_id=drive_id,
                       folder_id=folder_id,
                       deleted_at=(datetime(2026, 1, 1, tzinfo=timezone.utc)
                                   if deleted else None))
        db.session.add(f)
        db.session.commit()
        return f.id


def test_browse_lists_folders_and_files(auth_client, app, user, enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        folder = Folder(drive_id=drive_id, name="Docs", user_id=user)
        db.session.add(folder)
        db.session.commit()
        folder_id = folder.id
    root_file = _stored(app, user, drive_id, "root.txt")
    inner_file = _stored(app, user, drive_id, "inner.csv", folder_id)
    _stored(app, user, drive_id, "trashed.txt", deleted=True)

    resp = auth_client.get(f"/m/automation/api/browse?drive_id={drive_id}")
    data = resp.get_json()
    assert [f["name"] for f in data["folders"]] == ["Docs"]
    assert [f["name"] for f in data["files"]] == ["root.txt"]  # trash hidden

    resp = auth_client.get(
        f"/m/automation/api/browse?drive_id={drive_id}&folder_id={folder_id}")
    data = resp.get_json()
    assert [f["name"] for f in data["files"]] == ["inner.csv"]
    assert [c["name"] for c in data["crumbs"]] == ["Docs"]

    resp = auth_client.get(f"/m/automation/api/browse?file_id={inner_file}")
    assert resp.get_json()["name"] == "inner.csv"
    assert auth_client.get(
        f"/m/automation/api/browse?file_id={root_file}").status_code == 200


def test_browse_enforces_ownership(auth_client, app, user, enabled):
    with app.app_context():
        other = User(username="dave", email="dave@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        drive = Drive(name="Not yours", user_id=other.id)
        db.session.add(drive)
        db.session.commit()
        drive_id = drive.id
        f = StoredFile(name="secret.txt", stored_name="blob_secret",
                       user_id=other.id, drive_id=drive_id)
        db.session.add(f)
        db.session.commit()
        fid = f.id
    assert auth_client.get(
        f"/m/automation/api/browse?drive_id={drive_id}").status_code == 404
    assert auth_client.get(
        f"/m/automation/api/browse?file_id={fid}").status_code == 404


def test_folders_endpoint_lists_paths(auth_client, app, user, enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        root = Folder(drive_id=drive_id, name="Docs", user_id=user)
        db.session.add(root)
        db.session.flush()
        sub = Folder(drive_id=drive_id, name="2026", user_id=user,
                     parent_id=root.id)
        db.session.add(sub)
        db.session.commit()
    resp = auth_client.get(f"/m/automation/api/folders?drive_id={drive_id}")
    assert resp.status_code == 200
    paths = [f["path"] for f in resp.get_json()["folders"]]
    assert paths == ["Docs", "Docs/2026"]
    assert auth_client.get(
        "/m/automation/api/folders?drive_id=999999").status_code == 404


def test_folder_field_specs_use_picker():
    specs = {t: e["spec"] for t, e in NODE_TYPES.items()}
    by_name = {f["name"]: f for f in
               specs["output.save_file"]["fields"]}
    assert by_name["drive_id"]["kind"] == "drive"
    assert by_name["folder"]["kind"] == "folder"
    picks = {f["name"]: f for f in
             specs["input.folder_list"]["fields"]}
    assert picks["drive_id"]["kind"] == "drive"
    assert picks["folder_id"]["kind"] == "folder"


# ---------------------------------------------------------------------------
# Preview endpoint (editor right-click "run up to this node")
# ---------------------------------------------------------------------------

def test_preview_executes_subgraph_without_run(auth_client, app, user,
                                               enabled):
    graph = _text_graph("preview me")
    # A third, unconnected node that must NOT run in the sub-graph.
    graph["nodes"].append({"id": "n9", "type": "input.text",
                           "x": 0, "y": 0,
                           "config": {"content": "unused", "format": "text"}})
    wf_id = _workflow(app, user, graph)

    subgraph = {"nodes": [n for n in graph["nodes"] if n["id"] != "n9"],
                "edges": graph["edges"]}
    resp = auth_client.post(f"/m/automation/{wf_id}/preview",
                            json={"graph": subgraph})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert [e["node"] for e in data["log"]] == ["n1", "n2"]
    assert data["results"][0]["preview"] == "preview me"
    # Per-node outputs feed the editor's "View output" menu entry.
    assert data["outputs"]["n1"]["out"] == "preview me"
    assert data["outputs"]["n2"]["out"] == "preview me"
    # Preview runs are not persisted.
    with app.app_context():
        assert models.AutomationRun.query.count() == 0


def test_preview_reports_node_error_with_log(auth_client, app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("{bad", fmt="json"))
    resp = auth_client.post(f"/m/automation/{wf_id}/preview",
                            json={"graph": _text_graph("{bad", fmt="json")})
    assert resp.status_code == 400
    data = resp.get_json()
    assert "not valid JSON" in data["error"]
    assert any(e["status"] == "error" for e in data["log"])


def test_preview_requires_ownership_and_graph(auth_client, app, user,
                                              enabled):
    with app.app_context():
        other = User(username="erin", email="erin@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        wf = models.AutomationWorkflow(user_id=other.id, name="Not yours",
                                       graph="")
        db.session.add(wf)
        db.session.commit()
        wf_id = wf.id
    resp = auth_client.post(f"/m/automation/{wf_id}/preview",
                            json={"graph": _text_graph()})
    assert resp.status_code == 404

    own_id = _workflow(app, user, _text_graph())
    resp = auth_client.post(f"/m/automation/{own_id}/preview",
                            json={"graph": {"nodes": [], "edges": []}})
    assert resp.status_code == 400
    assert "Nothing to execute" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# Trigger input (drive → Run workflow)
# ---------------------------------------------------------------------------

_TRIGGER_GRAPH = {"nodes": [
    {"id": "n1", "type": "input.trigger", "x": 0, "y": 0, "config": {}},
    {"id": "n2", "type": "output.result", "x": 0, "y": 0, "config": {}},
], "edges": [
    {"from": "n1", "from_port": "out", "to": "n2", "to_port": "in"}]}


def test_trigger_node_receives_run_file(auth_client, app, user, enabled):
    fid = _make_file(app, user, "note.txt", "trigger content")
    wf_id = _workflow(app, user, _TRIGGER_GRAPH)
    resp = auth_client.post(f"/m/automation/{wf_id}/run",
                            json={"file_id": fid})
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]
    with app.app_context():
        run = db.session.get(models.AutomationRun, run_id)
        assert run.status == "done"
        assert run.trigger_file_id == fid
        out = json.loads(run.output)
        assert out["results"][0]["preview"] == "trigger content"


def test_trigger_node_without_file_fails(app, user):
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="no trigger input"):
            _execute({"nodes": [{"id": "t", "type": "input.trigger",
                                 "config": {}}], "edges": []}, app, user)


def test_run_with_foreign_file_rejected(auth_client, app, user, enabled):
    with app.app_context():
        other = User(username="fred", email="fred@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    fid = _make_file(app, other_id, "secret.txt", "nope")
    wf_id = _workflow(app, user, _TRIGGER_GRAPH)
    resp = auth_client.post(f"/m/automation/{wf_id}/run",
                            json={"file_id": fid})
    assert resp.status_code == 404


def test_for_file_api_lists_only_triggerable(auth_client, app, user, enabled):
    _workflow(app, user, _text_graph(), name="Plain")
    _workflow(app, user, _TRIGGER_GRAPH, name="Triggerable")
    resp = auth_client.get("/m/automation/api/for-file")
    names = [w["name"] for w in resp.get_json()["workflows"]]
    assert names == ["Triggerable"]


# ---------------------------------------------------------------------------
# Read file contents node
# ---------------------------------------------------------------------------

def test_read_files_concatenates_contents(app, user):
    f1 = _make_file(app, user, "a.txt", "alpha")
    f2 = _make_file(app, user, "b.txt", "beta")
    with app.app_context():
        logs = []
        ctx = {"user": db.session.get(User, user), "app": app,
               "log": logs.append}
        out = nodes._exec_read_files(
            ctx, {"max_chars": 6000},
            {"in": engine.p_files([{"file_id": f1, "name": "a.txt"},
                                   {"file_id": f2, "name": "b.txt"}])})
    text = out["data"]
    assert "=== a.txt ===" in text and "alpha" in text
    assert "=== b.txt ===" in text and "beta" in text


# ---------------------------------------------------------------------------
# AI nodes: system prompt + agent
# ---------------------------------------------------------------------------

def test_ai_prompt_system_message_first(app, user):
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.ai_service.chat_completion",
                   return_value={"role": "assistant",
                                 "content": "ok"}) as call:
            _execute({"nodes": [
                {"id": "ai", "type": "ai.prompt",
                 "config": {"prompt": "do it", "system": "be terse"}},
                {"id": "out", "type": "output.result", "config": {}},
            ], "edges": [{"from": "ai", "from_port": "out",
                          "to": "out", "to_port": "in"}]}, app, user)
        msgs = call.call_args[0][0]
        assert msgs[0] == {"role": "system", "content": "be terse"}
        assert msgs[1]["content"] == "do it"


def test_ai_agent_node_runs_agent(app, user):
    def fake_events(user_, history, drive=None, **kwargs):
        assert history[-1]["content"].startswith("find stuff")
        yield ("step", {"label": "Searching files"})
        yield ("answer", "agent answer")

    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.agent_service.run_agent_events",
                   fake_events):
            outputs, _, _ = _execute({"nodes": [
                {"id": "ai", "type": "ai.agent",
                 "config": {"prompt": "find stuff", "system": "be terse"}},
                {"id": "out", "type": "output.result", "config": {}},
            ], "edges": [{"from": "ai", "from_port": "out",
                          "to": "out", "to_port": "in"}]}, app, user)
        assert outputs["out"]["out"]["data"] == "agent answer"


# ---------------------------------------------------------------------------
# Notebook nodes
# ---------------------------------------------------------------------------

@pytest.fixture()
def notebooks_enabled(app, enabled):
    with app.app_context():
        db.session.add(ModuleState(name="notebooks", enabled=True))
        db.session.commit()


def test_notebook_input_reads_content(app, user, notebooks_enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        stored = nb_doc.create_notebook(
            user_obj, drive, "Log",
            cells=[nb_doc.new_cell("markdown", "# old entry")])
        nb_id = stored.id
        outputs, _, _ = _execute({"nodes": [
            {"id": "n", "type": "notebook.input",
             "config": {"notebook_id": nb_id}}], "edges": []}, app, user)
        assert "old entry" in outputs["n"]["out"]["data"]


def test_notebook_output_appends_and_creates(app, user, notebooks_enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        stored = nb_doc.create_notebook(
            user_obj, drive, "Log",
            cells=[nb_doc.new_cell("markdown", "# old entry")])
        nb_id = stored.id
        # Append to the existing notebook.
        _execute({"nodes": [
            {"id": "t", "type": "input.text",
             "config": {"content": "new cell", "format": "text"}},
            {"id": "o", "type": "notebook.output",
             "config": {"notebook_id": nb_id, "mode": "append"}},
        ], "edges": [{"from": "t", "from_port": "out",
                      "to": "o", "to_port": "in"}]}, app, user)
        text = nb_doc.extract(db.session.get(StoredFile, nb_id))
        assert "new cell" in text and "old entry" in text
        # Create a brand-new notebook.
        _execute({"nodes": [
            {"id": "t", "type": "input.text",
             "config": {"content": "fresh", "format": "text"}},
            {"id": "o", "type": "notebook.output",
             "config": {"notebook_id": "", "mode": "append",
                        "name": "Fresh NB", "drive_id": drive_id}},
        ], "edges": [{"from": "t", "from_port": "out",
                      "to": "o", "to_port": "in"}]}, app, user)
        names = [n.name for n in nb_doc.list_notebooks(user)]
        assert any("Fresh" in n for n in names)  # saved as Fresh_NB.pdocnb


def test_notebook_nodes_gated_by_module(app):
    # Without the notebooks module enabled the types are inactive.
    assert "notebook.input" in NODE_TYPES
    with app.test_request_context():
        active = registry.active_node_types()
    assert "notebook.input" not in active
    assert "input.trigger" in active  # automation core is always active


def test_notebook_output_updates_specific_cell(app, user, notebooks_enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        first = nb_doc.new_cell("markdown", "# keep me")
        second = nb_doc.new_cell("markdown", "old summary")
        stored = nb_doc.create_notebook(
            user_obj, drive, "Weekly", cells=[first, second])
        nb_id = stored.id
        _execute({"nodes": [
            {"id": "t", "type": "input.text",
             "config": {"content": "fresh summary", "format": "text"}},
            {"id": "o", "type": "notebook.output",
             "config": {"notebook_id": nb_id, "mode": "append",
                        "cell_id": second["id"]}},
        ], "edges": [{"from": "t", "from_port": "out",
                      "to": "o", "to_port": "in"}]}, app, user)
        document = nb_doc.load(db.session.get(StoredFile, nb_id))
        assert len(document["cells"]) == 2  # replaced, not appended
        assert document["cells"][0]["content"] == "# keep me"
        assert document["cells"][1]["content"] == "fresh summary"
        assert document["cells"][1]["id"] == second["id"]  # same cell


def test_notebook_output_stale_cell_id_errors(app, user, notebooks_enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        stored = nb_doc.create_notebook(
            user_obj, drive, "Weekly",
            cells=[nb_doc.new_cell("markdown", "# entry")])
        nb_id = stored.id
        with pytest.raises(engine.WorkflowError, match="no longer exists"):
            _execute({"nodes": [
                {"id": "t", "type": "input.text",
                 "config": {"content": "x", "format": "text"}},
                {"id": "o", "type": "notebook.output",
                 "config": {"notebook_id": nb_id, "cell_id": "gone123"}},
            ], "edges": [{"from": "t", "from_port": "out",
                          "to": "o", "to_port": "in"}]}, app, user)


def test_notebook_cells_endpoint(auth_client, app, user, enabled,
                                 notebooks_enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        stored = nb_doc.create_notebook(
            user_obj, drive, "Weekly",
            cells=[nb_doc.new_cell("markdown", "# Summary\nnotes here"),
                   nb_doc.new_cell("todo", "call Ana")])
        nb_id = stored.id
        # Another user's notebook must not leak through the endpoint.
        other = User(username="bob", email="bob@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        other_nb = nb_doc.create_notebook(
            other, drive, "Bob notes",
            cells=[nb_doc.new_cell("markdown", "secret")]).id

    resp = auth_client.get(f"/m/automation/api/notebook-cells"
                           f"?notebook_id={nb_id}")
    assert resp.status_code == 200
    cells = resp.get_json()["cells"]
    assert [c["index"] for c in cells] == [1, 2]
    assert cells[0]["type"] == "markdown"
    assert "Summary" in cells[0]["preview"]
    assert cells[1]["type"] == "todo"

    resp = auth_client.get(f"/m/automation/api/notebook-cells"
                           f"?notebook_id={other_nb}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Examples gallery
# ---------------------------------------------------------------------------

def test_example_use_creates_workflow(auth_client, app, user, enabled):
    resp = auth_client.post("/m/automation/examples/translate-file/use")
    assert resp.status_code == 302
    with app.app_context():
        wf = (models.AutomationWorkflow.query
              .filter_by(name="Translate a file").one())
        graph = json.loads(wf.graph)
        assert graph["nodes"][0]["type"] == "input.trigger"


def test_example_use_unknown_key_404(auth_client, app, user, enabled):
    assert auth_client.post(
        "/m/automation/examples/nope/use").status_code == 404


def test_all_examples_are_valid_graphs():
    for ex in examples.EXAMPLES:
        graph = engine.parse_graph(json.dumps(ex["graph"]))
        engine.validate_graph(graph, NODE_TYPES)


# ---------------------------------------------------------------------------
# AI tools: catalog + create_workflow
# ---------------------------------------------------------------------------

def test_node_catalog_tool_lists_types(app, user):
    with app.app_context():
        result = tools._tool_node_catalog(
            db.session.get(User, user), {}, None)
    types = {t["type"] for t in result["node_types"]}
    assert "input.trigger" in types and "ai.agent" in types
    assert "graph_format" in result


def test_create_workflow_tool(app, user, enabled):
    with app.app_context():
        result = tools._tool_create(db.session.get(User, user), {
            "name": "AI built",
            "graph": {"nodes": [
                {"id": "n1", "type": "input.text",
                 "config": {"content": "hi", "format": "text"}},
                {"id": "n2", "type": "output.result", "config": {}},
            ], "edges": [
                {"from": "n1", "from_port": "out",
                 "to": "n2", "to_port": "in"}]}}, None)
        assert result["ok"] is True
        wf = db.session.get(models.AutomationWorkflow,
                            result["workflow_id"])
        assert wf.name == "AI built"


def test_create_workflow_tool_rejects_bad_graph(app, user, enabled):
    with app.app_context():
        result = tools._tool_create(db.session.get(User, user), {
            "name": "Broken",
            "graph": {"nodes": [{"id": "n1", "type": "bogus.node",
                                 "config": {}}], "edges": []}}, None)
        assert "error" in result
        assert "Unknown node type" in result["error"]


# ---------------------------------------------------------------------------
# JSON to table node + file-shape endpoint (editor live hints)
# ---------------------------------------------------------------------------

def _json_to_table_graph(content):
    return {"nodes": [
        {"id": "n1", "type": "input.text",
         "config": {"content": content, "format": "json"}},
        {"id": "n2", "type": "transform.json_to_table", "config": {}},
    ], "edges": [
        {"from": "n1", "from_port": "out", "to": "n2", "to_port": "in"}]}


def test_json_to_table_from_list_of_objects():
    graph = _json_to_table_graph(
        '[{"name": "Ana", "age": 30}, {"name": "Rui", "city": "Porto"}]')
    outputs, _, _ = _execute(graph)
    table = outputs["n2"]["out"]["data"]
    assert table["columns"] == ["name", "age", "city"]
    assert table["rows"] == [["Ana", 30, ""], ["Rui", "", "Porto"]]


def test_json_to_table_from_single_object():
    outputs, _, _ = _execute(_json_to_table_graph('{"a": 1, "b": {"x": 2}}'))
    table = outputs["n2"]["out"]["data"]
    assert table["columns"] == ["key", "value"]
    assert ["b", '{"x": 2}'] in table["rows"]


def test_json_to_table_rejects_scalars_and_empty():
    with pytest.raises(engine.WorkflowError, match="nothing to tabulate"):
        _execute(_json_to_table_graph('[1, 2, 3]'))
    with pytest.raises(engine.WorkflowError, match="empty"):
        _execute(_json_to_table_graph('[]'))
    with pytest.raises(engine.WorkflowError, match="No input"):
        _execute({"nodes": [{"id": "n2", "type": "transform.json_to_table",
                             "config": {}}],
                  "edges": []})


def test_file_shape_endpoint_csv_and_json(auth_client, app, user, enabled):
    csv_id = _make_file(app, user, "people.csv", "name,age\nAna,30\n")
    json_id = _make_file(app, user, "data.json",
                         '{"user": {"name": "Ana"}, "tags": ["a"]}')
    txt_id = _make_file(app, user, "notes.txt", "just text")

    resp = auth_client.get(f"/m/automation/api/file-shape?file_id={csv_id}")
    assert resp.get_json() == {"kind": "table",
                               "columns": ["name", "age"], "keys": []}

    resp = auth_client.get(f"/m/automation/api/file-shape?file_id={json_id}")
    assert resp.get_json() == {"kind": "json", "columns": [],
                               "keys": ["user", "tags"]}

    resp = auth_client.get(f"/m/automation/api/file-shape?file_id={txt_id}")
    assert resp.get_json() == {"kind": "text", "columns": [], "keys": []}


def test_file_shape_endpoint_enforces_ownership(auth_client, app, user,
                                                enabled):
    with app.app_context():
        other = User(username="eve", email="eve@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        drive = Drive(name="Private", user_id=other.id)
        db.session.add(drive)
        db.session.commit()
        f = StoredFile(name="secret.csv", stored_name="blob_secret",
                       user_id=other.id, drive_id=drive.id)
        db.session.add(f)
        db.session.commit()
        fid = f.id
    assert auth_client.get(
        f"/m/automation/api/file-shape?file_id={fid}").status_code == 404


def test_every_node_type_has_description_and_example():
    for type_name, entry in NODE_TYPES.items():
        spec = entry["spec"]
        assert spec.get("description"), f"{type_name} has no description"
        assert spec.get("example"), f"{type_name} has no example"


def test_table_filter_multiple_conditions(app, user):
    fid = _make_file(app, user, "sales.csv",
                     "name,status,total\na,paid,50\nb,paid,150\n"
                     "c,pending,200\n")

    def run(config):
        graph = {"nodes": [
            {"id": "f", "type": "input.file",
             "config": {"file_id": str(fid)}},
            {"id": "flt", "type": "transform.table_filter",
             "config": config},
        ], "edges": [
            {"from": "f", "from_port": "out", "to": "flt", "to_port": "in"}]}
        outputs, _, _ = _execute(graph, app, user)
        return outputs["flt"]["out"]["data"]["rows"]

    with app.app_context():
        # Two conditions, match ALL (the default).
        rows = run({"conditions": [
            {"column": "status", "op": "equals", "value": "paid"},
            {"column": "total", "op": "gt", "value": "100"}]})
        assert rows == [["b", "paid", "150"]]

        # Two conditions, match ANY.
        rows = run({"match": "any", "conditions": [
            {"column": "status", "op": "equals", "value": "pending"},
            {"column": "total", "op": "lt", "value": "60"}]})
        assert rows == [["a", "paid", "50"], ["c", "pending", "200"]]

        # Empty condition rows are ignored; none left -> clear error.
        with pytest.raises(engine.WorkflowError,
                           match="at least one condition"):
            run({"conditions": [{"column": "", "op": "equals",
                                 "value": ""}]})

        # IN: comma-separated list on the right (case-insensitive).
        rows = run({"conditions": [
            {"column": "status", "op": "in", "value": "Paid, Pending"}]})
        assert len(rows) == 3
        rows = run({"conditions": [
            {"column": "status", "op": "in", "value": "paid"}]})
        assert rows == [["a", "paid", "50"], ["b", "paid", "150"]]

        # NOT IN.
        rows = run({"conditions": [
            {"column": "status", "op": "not_in", "value": "paid"}]})
        assert rows == [["c", "pending", "200"]]


def test_table_filter_legacy_single_condition(app, user):
    """Old configs (column/op/value fields) keep working unchanged."""
    fid = _make_file(app, user, "data.csv", "name,age\nAda,36\nAlan,41\n")
    with app.app_context():
        graph = {"nodes": [
            {"id": "f", "type": "input.file",
             "config": {"file_id": str(fid)}},
            {"id": "flt", "type": "transform.table_filter",
             "config": {"column": "age", "op": "gt", "value": "40"}},
        ], "edges": [
            {"from": "f", "from_port": "out", "to": "flt", "to_port": "in"}]}
        outputs, _, _ = _execute(graph, app, user)
        assert outputs["flt"]["out"]["data"]["rows"] == [["Alan", "41"]]


def test_table_select_picks_with_rename(app, user):
    fid = _make_file(app, user, "data.csv",
                     "name,age,city\nAna,30,Porto\nRui,41,Lisboa\n")
    with app.app_context():
        graph = {"nodes": [
            {"id": "f", "type": "input.file",
             "config": {"file_id": str(fid)}},
            {"id": "sel", "type": "transform.table_select",
             "config": {"picks": [{"column": "city", "alias": ""},
                                  {"column": "name", "alias": "Person"}]}},
        ], "edges": [
            {"from": "f", "from_port": "out", "to": "sel", "to_port": "in"}]}
        outputs, _, _ = _execute(graph, app, user)
        table = outputs["sel"]["out"]["data"]
        assert table["columns"] == ["city", "Person"]
        assert table["rows"] == [["Porto", "Ana"], ["Lisboa", "Rui"]]

        with pytest.raises(engine.WorkflowError,
                           match="at least one column"):
            _execute({"nodes": [
                {"id": "f", "type": "input.file",
                 "config": {"file_id": str(fid)}},
                {"id": "sel", "type": "transform.table_select",
                 "config": {"picks": [{"column": "", "alias": ""}]}},
            ], "edges": [
                {"from": "f", "from_port": "out",
                 "to": "sel", "to_port": "in"}]}, app, user)


# ---------------------------------------------------------------------------
# Run inputs (input.param) — F1
# ---------------------------------------------------------------------------

def _param_graph(config, fmt="text"):
    return {"nodes": [
        {"id": "p", "type": "input.param", "config": config},
        {"id": "out", "type": "output.result", "config": {}},
    ], "edges": [
        {"from": "p", "from_port": "out", "to": "out", "to_port": "in"}]}


def test_input_param_uses_answered_value(app, user):
    graph = _param_graph({"param": "question", "kind": "text", "ask": "yes"})
    with app.app_context():
        outputs, _, _ = engine.execute_graph(
            graph, dict(_ctx(app, user), params={"question": "why?"}),
            node_types=NODE_TYPES)
        assert outputs["p"]["out"]["data"] == "why?"


def test_input_param_falls_back_to_static_default(app, user):
    graph = _param_graph({"param": "region", "kind": "text", "ask": "yes",
                          "default": "north"})
    with app.app_context():
        outputs, _, _ = _execute(graph, app, user)  # no params answered
        assert outputs["p"]["out"]["data"] == "north"


def test_input_param_required_and_missing_errors(app, user):
    graph = _param_graph({"param": "question", "kind": "text", "ask": "yes"})
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="was not provided"):
            _execute(graph, app, user)


def test_input_param_ask_no_never_asks(app, user):
    graph = _param_graph({"param": "mode", "kind": "text", "ask": "no",
                          "default": "silent"})
    with app.app_context():
        # Even an answered param is ignored when asking is off.
        outputs, _, _ = engine.execute_graph(
            graph, dict(_ctx(app, user), params={"mode": "loud"}),
            node_types=NODE_TYPES)
        assert outputs["p"]["out"]["data"] == "silent"


def test_input_param_file_kind_loads_static_file(app, user):
    fid = _make_file(app, user, "notes.txt", "file body")
    graph = _param_graph({"param": "doc", "kind": "file", "ask": "no",
                          "file_id": str(fid)})
    with app.app_context():
        outputs, _, _ = _execute(graph, app, user)
        assert outputs["p"]["out"]["data"] == "file body"


def test_input_param_file_kind_without_file_errors(app, user):
    graph = _param_graph({"param": "doc", "kind": "file", "ask": "no"})
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="has no file"):
            _execute(graph, app, user)


def test_input_param_without_name_errors(app, user):
    graph = _param_graph({"param": "", "kind": "text", "ask": "no",
                          "default": "x"})
    with pytest.raises(engine.WorkflowError, match="parameter name"):
        engine.validate_graph(graph, NODE_TYPES)


def test_validate_rejects_duplicate_param_names():
    graph = {"nodes": [
        {"id": "a", "type": "input.param",
         "config": {"param": "q", "kind": "text", "ask": "no",
                    "default": "1"}},
        {"id": "b", "type": "input.param",
         "config": {"param": "q", "kind": "text", "ask": "no",
                    "default": "2"}},
    ], "edges": []}
    with pytest.raises(engine.WorkflowError, match="must be unique"):
        engine.validate_graph(graph, NODE_TYPES)


def test_run_param_specs_marks_required_and_skips_static():
    graph = {"nodes": [
        {"id": "a", "type": "input.param",
         "config": {"param": "q", "label": "Question?", "kind": "text",
                    "ask": "yes"}},
        {"id": "b", "type": "input.param",
         "config": {"param": "region", "kind": "text", "ask": "yes",
                    "default": "north"}},
        {"id": "c", "type": "input.param",
         "config": {"param": "mode", "kind": "text", "ask": "no",
                    "default": "x"}},
    ], "edges": []}
    specs = nodes.run_param_specs(graph)
    assert [s["param"] for s in specs] == ["q", "region"]
    assert specs[0] == {"param": "q", "label": "Question?", "kind": "text",
                        "required": True}
    assert specs[1]["required"] is False  # static default = fallback


def test_run_endpoint_requires_missing_params(auth_client, app, user,
                                              enabled):
    wf_id = _workflow(app, user, _param_graph(
        {"param": "question", "label": "Question?", "kind": "text",
         "ask": "yes"}))
    resp = auth_client.post(f"/m/automation/{wf_id}/run", json={})
    assert resp.status_code == 400
    assert "Question?" in resp.get_json()["error"]
    resp = auth_client.post(f"/m/automation/{wf_id}/run",
                            json={"params": {"question": "why"}})
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]
    with app.app_context():
        run = db.session.get(models.AutomationRun, run_id)
        assert json.loads(run.params) == {"question": "why"}
        assert run.status == "done"


def test_run_endpoint_validates_file_param(auth_client, app, user, enabled):
    wf_id = _workflow(app, user, _param_graph(
        {"param": "doc", "label": "Document", "kind": "file", "ask": "yes"}))
    resp = auth_client.post(f"/m/automation/{wf_id}/run",
                            json={"params": {"doc": "not-a-number"}})
    assert resp.status_code == 400
    resp = auth_client.post(f"/m/automation/{wf_id}/run",
                            json={"params": {"doc": "99999"}})
    assert resp.status_code == 404
    fid = _make_file(app, user, "ok.txt", "content")
    resp = auth_client.post(f"/m/automation/{wf_id}/run",
                            json={"params": {"doc": str(fid)}})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Behaviors moved to core (/behaviors) — the old module URL redirects.
# CRUD/toggle/preset coverage lives in tests/test_behaviors.py.
# ---------------------------------------------------------------------------

def test_legacy_behaviors_url_redirects(auth_client, app, user, enabled):
    resp = auth_client.get("/m/automation/behaviors")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/behaviors/")


def test_module_tools_offer_namespaced_names(app, user, enabled):
    """Regression: tool definitions offered to the model must carry the full
    namespaced name — the model calls back exactly the name it was offered,
    so a plain name ('get_workflow') makes every call 'Unknown tool'."""
    from app.services import agent_service
    with app.app_context():
        defs, handlers, _ = agent_service._active_tools()
    names = [d["function"]["name"] for d in defs]
    assert "automation.get_workflow" in names
    assert "automation.edit_workflow" in names
    assert "automation.list_workflows" in names
    assert "get_workflow" not in names  # plain names must never leak
    for d in defs:  # every offered tool is dispatchable
        assert d["function"]["name"] in handlers


# ---------------------------------------------------------------------------
# Pandas table nodes — F3
# ---------------------------------------------------------------------------

def _table_file_graph(fid, node_type, config, extra_node=None,
                      extra_edge=None):
    graph_nodes = [
        {"id": "f", "type": "input.file", "config": {"file_id": str(fid)}},
        {"id": "t", "type": node_type, "config": config},
    ]
    graph_edges = [
        {"from": "f", "from_port": "out", "to": "t",
         "to_port": "in1" if extra_node else "in"}]
    if extra_node:
        graph_nodes.append(extra_node)
        graph_edges.append(extra_edge)
    return {"nodes": graph_nodes, "edges": graph_edges}


def test_table_aggregate_grouped(app, user):
    fid = _make_file(app, user, "sales.csv",
                     "region,total\nN,10\nS,20\nN,30\n")
    with app.app_context():
        outputs, _, _ = _execute(_table_file_graph(
            fid, "transform.table_aggregate",
            {"groups": ["region"],
             "aggs": [{"column": "total", "func": "sum", "as": "Revenue"}]}),
            app, user)
        table = outputs["t"]["out"]["data"]
        assert table["columns"] == ["region", "Revenue"]
        assert table["rows"] == [["N", 40], ["S", 20]]


def test_table_aggregate_without_groups_single_row(app, user):
    fid = _make_file(app, user, "n.csv", "v\n5\n15\n")
    with app.app_context():
        outputs, _, _ = _execute(_table_file_graph(
            fid, "transform.table_aggregate",
            {"aggs": [{"column": "v", "func": "avg", "as": ""}]}), app, user)
        table = outputs["t"]["out"]["data"]
        assert table["columns"] == ["v_avg"]
        assert table["rows"] == [[10.0]]


def test_table_aggregate_requires_aggregation(app, user):
    fid = _make_file(app, user, "n.csv", "v\n5\n")
    with app.app_context():
        with pytest.raises(engine.WorkflowError,
                           match="at least one aggregation"):
            _execute(_table_file_graph(
                fid, "transform.table_aggregate", {"aggs": []}), app, user)


def test_table_join_inner_and_left(app, user):
    left = _make_file(app, user, "orders.csv", "cid,total\n1,50\n2,70\n3,90\n")
    right = _make_file(app, user, "customers.csv", "id,name\n1,Ana\n2,Rui\n")

    def run(how):
        graph = _table_file_graph(
            left, "transform.table_join",
            {"left_on": "cid", "right_on": "id", "how": how},
            extra_node={"id": "g", "type": "input.file",
                        "config": {"file_id": str(right)}},
            extra_edge={"from": "g", "from_port": "out", "to": "t",
                        "to_port": "in2"})
        outputs, _, _ = _execute(graph, app, user)
        return outputs["t"]["out"]["data"]

    with app.app_context():
        inner = run("inner")
        assert inner["columns"] == ["cid", "total", "id", "name"]
        assert inner["rows"] == [[1, 50, 1, "Ana"],
                                 [2, 70, 2, "Rui"]]
        left_join = run("left")
        assert len(left_join["rows"]) == 3  # order 3 kept, name empty
        assert left_join["rows"][2][0] == 3


def test_table_join_unknown_column_errors(app, user):
    left = _make_file(app, user, "a.csv", "x\n1\n")
    right = _make_file(app, user, "b.csv", "y\n2\n")
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="does not exist"):
            _execute(_table_file_graph(
                left, "transform.table_join",
                {"left_on": "nope", "right_on": "y"},
                extra_node={"id": "g", "type": "input.file",
                            "config": {"file_id": str(right)}},
                extra_edge={"from": "g", "from_port": "out", "to": "t",
                            "to_port": "in2"}), app, user)


def test_table_sort_descending(app, user):
    fid = _make_file(app, user, "n.csv", "name,v\na,3\nb,1\nc,2\n")
    with app.app_context():
        outputs, _, _ = _execute(_table_file_graph(
            fid, "transform.table_sort",
            {"column": "v", "direction": "desc"}), app, user)
        rows = outputs["t"]["out"]["data"]["rows"]
        assert [r[0] for r in rows] == ["a", "c", "b"]


def test_table_dedupe_on_subset(app, user):
    fid = _make_file(app, user, "d.csv",
                     "email,note\na@x,1\na@x,2\nb@x,3\n")
    with app.app_context():
        outputs, _, _ = _execute(_table_file_graph(
            fid, "transform.table_dedupe", {"columns": ["email"]}),
            app, user)
        rows = outputs["t"]["out"]["data"]["rows"]
        assert rows == [["a@x", 1], ["b@x", 3]]


def test_table_compute_new_column(app, user):
    fid = _make_file(app, user, "p.csv", "price,qty\n10,2\n5,4\n")
    with app.app_context():
        outputs, _, _ = _execute(_table_file_graph(
            fid, "transform.table_compute",
            {"new_column": "total", "formula": "price * qty"}), app, user)
        table = outputs["t"]["out"]["data"]
        assert table["columns"] == ["price", "qty", "total"]
        assert [r[2] for r in table["rows"]] == [20, 20]


def test_table_compute_bad_formula_errors(app, user):
    fid = _make_file(app, user, "p.csv", "price\n10\n")
    with app.app_context():
        with pytest.raises(engine.WorkflowError,
                           match="could not be evaluated"):
            _execute(_table_file_graph(
                fid, "transform.table_compute",
                {"new_column": "x", "formula": "price *"}), app, user)


def test_save_table_csv_creates_file(auth_client, app, user, enabled):
    drive_id = _drive(app, user)
    fid = _make_file(app, user, "t.csv", "a,b\n1,2\n")
    graph = {"nodes": [
        {"id": "f", "type": "input.file", "config": {"file_id": str(fid)}},
        {"id": "s", "type": "output.save_table",
         "config": {"name": "report", "format": "csv",
                    "drive_id": str(drive_id)}},
    ], "edges": [
        {"from": "f", "from_port": "out", "to": "s", "to_port": "in"}]}
    with app.app_context():
        _, _, created = _execute(graph, app, user)
        assert len(created) == 1  # extension fixed to .csv
        assert created[0]["name"] == "report.csv"
        stored = db.session.get(StoredFile, created[0]["file_id"])
        assert stored.drive_id == drive_id


def test_save_table_refuses_synced_drive(app, user):
    drive_id = _drive(app, user, synced=True)
    fid = _make_file(app, user, "t.csv", "a\n1\n")
    graph = {"nodes": [
        {"id": "f", "type": "input.file", "config": {"file_id": str(fid)}},
        {"id": "s", "type": "output.save_table",
         "config": {"name": "t.csv", "drive_id": str(drive_id)}},
    ], "edges": [
        {"from": "f", "from_port": "out", "to": "s", "to_port": "in"}]}
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="read-only"):
            _execute(graph, app, user)


# ---------------------------------------------------------------------------
# Image nodes — F4
# ---------------------------------------------------------------------------

def _make_image(app, user_id, name="pic.png"):
    import io as _io
    from PIL import Image
    from app.services import file_service

    buf = _io.BytesIO()
    Image.new("RGB", (4, 2), "red").save(buf, format="PNG")

    class FakeStorage:
        filename = name
        mimetype = "image/png"

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(buf.getvalue())

    with app.app_context():
        user = db.session.get(User, user_id)
        return file_service.save_upload(FakeStorage(), user).id


def test_image_info_reads_metadata(app, user):
    fid = _make_image(app, user)
    with app.app_context():
        outputs, _, _ = _execute({"nodes": [
            {"id": "i", "type": "transform.image_info",
             "config": {"file_id": str(fid)}},
        ], "edges": []}, app, user)
        info = outputs["i"]["out"]["data"]
        assert info["width"] == 4 and info["height"] == 2
        assert info["format"] == "PNG"


def test_image_nodes_reject_non_image(app, user):
    fid = _make_file(app, user, "doc.txt", "hello")
    with app.app_context():
        with pytest.raises(engine.WorkflowError, match="not an image"):
            _execute({"nodes": [
                {"id": "i", "type": "transform.image_info",
                 "config": {"file_id": str(fid)}},
            ], "edges": []}, app, user)


def test_caption_image_with_mocked_ai(app, user):
    fid = _make_image(app, user)
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value=dict(_FAKE_CFG)), \
             patch("app.services.ai_service.caption_image",
                   return_value="a red square") as cap:
            outputs, _, _ = _execute({"nodes": [
                {"id": "c", "type": "ai.caption_image",
                 "config": {"file_id": str(fid),
                            "instruction": "Describe it."}},
            ], "edges": []}, app, user)
        assert outputs["c"]["out"]["data"] == "a red square"
        assert cap.call_args.kwargs["instruction"] == "Describe it."


def test_caption_image_requires_ai_config(app, user):
    fid = _make_image(app, user)
    with app.app_context():
        with patch("app.services.ai_service.config_for",
                   return_value={"enabled": False, "base_url": "",
                                 "model": ""}):
            with pytest.raises(engine.WorkflowError,
                               match="AI is not configured"):
                _execute({"nodes": [
                    {"id": "c", "type": "ai.caption_image",
                     "config": {"file_id": str(fid)}},
                ], "edges": []}, app, user)


def test_ocr_image_reports_missing_tesseract(app, user):
    fid = _make_image(app, user)
    with app.app_context():
        with patch("app.services.indexing_service.ocr_available",
                   return_value=False):
            with pytest.raises(engine.WorkflowError, match="tesseract"):
                _execute({"nodes": [
                    {"id": "o", "type": "transform.ocr_image",
                     "config": {"file_id": str(fid)}},
                ], "edges": []}, app, user)


def test_ocr_image_returns_text(app, user):
    fid = _make_image(app, user)
    with app.app_context():
        with patch("app.services.indexing_service.ocr_available",
                   return_value=True), \
             patch("app.services.indexing_service.ocr_image",
                   return_value="scanned words"):
            outputs, _, _ = _execute({"nodes": [
                {"id": "o", "type": "transform.ocr_image",
                 "config": {"file_id": str(fid)}},
            ], "edges": []}, app, user)
        assert outputs["o"]["out"]["data"] == "scanned words"


# ---------------------------------------------------------------------------
# Logic nodes — F5 (empty/not_empty and in/not_in on the condition node)
# ---------------------------------------------------------------------------

def _run_condition(config, content="value"):
    graph = {"nodes": [
        {"id": "n1", "type": "input.text",
         "config": {"content": content, "format": "text"}},
        {"id": "c", "type": "logic.condition", "config": config},
        {"id": "t", "type": "output.result", "config": {}},
        {"id": "f", "type": "output.result", "config": {}},
    ], "edges": [
        {"from": "n1", "from_port": "out", "to": "c", "to_port": "in"},
        {"from": "c", "from_port": "true", "to": "t", "to_port": "in"},
        {"from": "c", "from_port": "false", "to": "f", "to_port": "in"},
    ]}
    outputs, results, _ = _execute(graph)
    return outputs, results


def test_condition_empty_and_not_empty():
    outputs, results = _run_condition(
        {"left": "{{ n1 }}", "op": "not_empty"}, content="x")
    assert results[0]["node"] == "t"
    outputs, results = _run_condition(
        {"left": "{{ n1 }}", "op": "empty"}, content="   ")
    assert results[0]["node"] == "t"


def test_condition_in_and_not_in_lists():
    _, results = _run_condition(
        {"left": "{{ n1 }}", "op": "in", "right": "paid, Pending"},
        content="Paid")
    assert results[0]["node"] == "t"
    _, results = _run_condition(
        {"left": "{{ n1 }}", "op": "not_in", "right": "paid"},
        content="overdue")
    assert results[0]["node"] == "t"


# ---------------------------------------------------------------------------
# AI copilot tools — F6
# ---------------------------------------------------------------------------

def test_get_workflow_tool_inlines_specs(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("copilot"), name="Copilot flow")
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = tools._tool_get_workflow(user_obj, {"workflow_id": wf_id},
                                          None)
        assert result["workflow_id"] == wf_id
        assert result["name"] == "Copilot flow"
        assert len(result["graph"]["nodes"]) == 2
        # The types in use come with their port/config field specs.
        assert "input.text" in result["specs"]
        assert result["specs"]["input.text"]["outputs"] == [
            {"name": "out", "kind": "text"}]
        assert "error" in tools._tool_get_workflow(
            user_obj, {"workflow_id": 999}, None)


def test_edit_workflow_tool_adds_connects_and_updates(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("before"))
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = tools._tool_edit_workflow(user_obj, {
            "workflow_id": wf_id,
            "operations": [
                {"op": "add_node", "type": "transform.template",
                 "id": "tpl", "x": 300, "y": 40,
                 "config": {"template": "wrapped: {{ n1 }}"}},
                {"op": "update_node", "id": "n1",
                 "config": {"content": "after"}},
                {"op": "connect", "from": "n1", "from_port": "out",
                 "to": "tpl", "to_port": "in"},
                {"op": "disconnect", "from": "n1", "to": "n2"},
            ]}, None)
        assert result["ok"]
        assert result["ui"] == {"type": "workflow_changed",
                                "workflow_id": wf_id}
        graph = json.loads(
            db.session.get(models.AutomationWorkflow, wf_id).graph)
        by_id = {n["id"]: n for n in graph["nodes"]}
        assert by_id["tpl"]["config"]["template"] == "wrapped: {{ n1 }}"
        assert by_id["n1"]["config"]["content"] == "after"  # merged
        assert {"from": "n1", "from_port": "out", "to": "tpl",
                "to_port": "in"} in graph["edges"]
        assert not [e for e in graph["edges"] if e["to"] == "n2"]


def test_edit_workflow_tool_removes_node_and_its_edges(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("x"))
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = tools._tool_edit_workflow(user_obj, {
            "workflow_id": wf_id,
            "operations": [{"op": "remove_node", "id": "n2"}]}, None)
        assert result["ok"]
        graph = json.loads(
            db.session.get(models.AutomationWorkflow, wf_id).graph)
        assert [n["id"] for n in graph["nodes"]] == ["n1"]
        assert graph["edges"] == []


def test_edit_workflow_tool_rejects_breaking_edits(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("keep me"))
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = tools._tool_edit_workflow(user_obj, {
            "workflow_id": wf_id,
            "operations": [
                {"op": "add_node", "type": "bogus.node", "id": "bad"},
            ]}, None)
        assert "error" in result
        assert "Nothing was saved" in result["error"]
        # Graph untouched.
        graph = json.loads(
            db.session.get(models.AutomationWorkflow, wf_id).graph)
        assert [n["id"] for n in graph["nodes"]] == ["n1", "n2"]
        assert "error" in tools._tool_edit_workflow(
            user_obj, {"workflow_id": wf_id, "operations": []}, None)
        assert "error" in tools._tool_edit_workflow(
            user_obj, {"workflow_id": wf_id,
                       "operations": [{"op": "fly_to_moon"}]}, None)


def test_edit_workflow_tool_enforces_ownership(app, user, enabled):
    with app.app_context():
        other = User(username="other3", email="other3@x.com")
        other.set_password("x")
        db.session.add(other)
        db.session.commit()
        wf_id = _workflow(app, other.id, _text_graph("secret"))
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert "error" in tools._tool_edit_workflow(
            user_obj, {"workflow_id": wf_id,
                       "operations": [{"op": "remove_node", "id": "n1"}]},
            None)
        assert "error" in tools._tool_get_workflow(
            user_obj, {"workflow_id": wf_id}, None)


def test_graph_endpoint_returns_saved_graph(auth_client, app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("via endpoint"))
    resp = auth_client.get(f"/m/automation/{wf_id}/graph")
    assert resp.status_code == 200
    graph = resp.get_json()["graph"]
    assert [n["id"] for n in graph["nodes"]] == ["n1", "n2"]


def test_editor_exposes_graph_url_and_workflow_context(auth_client, app,
                                                       user, enabled):
    wf_id = _workflow(app, user, _text_graph())
    resp = auth_client.get(f"/m/automation/{wf_id}")
    assert resp.status_code == 200
    assert f"/m/automation/{wf_id}/graph".encode() in resp.data
    assert b"window.currentWorkflow" in resp.data


# ---------------------------------------------------------------------------
# AI access flags (lock / hide) — notebook-style
# ---------------------------------------------------------------------------

def test_list_workflows_hides_hidden_and_shows_lock(app, user, enabled):
    _workflow(app, user, _text_graph(), name="Plain")
    _workflow(app, user, _text_graph(), name="Locked", ai_lock=True)
    _workflow(app, user, _text_graph(), name="Secret", ai_hidden=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        listing = tools._tool_list(user_obj, {}, None)
        names = {w["name"] for w in listing["workflows"]}
        assert names == {"Plain", "Locked"}  # hidden workflow not listed
        by_name = {w["name"]: w for w in listing["workflows"]}
        assert by_name["Locked"]["ai_lock"] is True
        assert by_name["Plain"]["ai_lock"] is False


def test_hidden_workflow_refuses_all_ai_tools(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph(), ai_hidden=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        for call in (
                lambda: tools._tool_get_workflow(
                    user_obj, {"workflow_id": wf_id}, None),
                lambda: tools._tool_edit_workflow(
                    user_obj, {"workflow_id": wf_id,
                               "operations": [{"op": "remove_node",
                                               "id": "n1"}]}, None),
                lambda: tools._tool_run(
                    user_obj, {"workflow_id": wf_id}, None)):
            result = call()
            assert "error" in result
            assert "hidden from the AI" in result["error"]


def test_locked_workflow_refuses_edits_but_not_reads(app, user, enabled):
    wf_id = _workflow(app, user, _text_graph("locked"), ai_lock=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = tools._tool_edit_workflow(user_obj, {
            "workflow_id": wf_id,
            "operations": [{"op": "remove_node", "id": "n1"}]}, None)
        assert "error" in result
        assert "LOCKED" in result["error"]
        # Reads still work and surface the lock up front.
        got = tools._tool_get_workflow(user_obj, {"workflow_id": wf_id}, None)
        assert got["ai_lock"] is True
        assert "LOCKED" in got["note"]
        # Graph untouched.
        graph = json.loads(
            db.session.get(models.AutomationWorkflow, wf_id).graph)
        assert [n["id"] for n in graph["nodes"]] == ["n1", "n2"]


def test_ai_flags_route_toggles_and_enforces_ownership(
        auth_client, app, user, enabled):
    wf_id = _workflow(app, user, _text_graph())
    resp = auth_client.post(f"/m/automation/{wf_id}/ai-flags",
                            json={"ai_lock": True})
    assert resp.status_code == 200
    assert resp.get_json()["ai_lock"] is True
    resp = auth_client.post(f"/m/automation/{wf_id}/ai-flags",
                            json={"ai_hidden": True})
    assert resp.get_json()["ai_hidden"] is True
    with app.app_context():
        wf = db.session.get(models.AutomationWorkflow, wf_id)
        assert wf.ai_lock and wf.ai_hidden
    # Other users get 404.
    with app.app_context():
        other = User(username="other4", email="other4@x.com")
        other.set_password("x")
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    auth_client.get("/logout")
    auth_client.post("/login", data={"username": "other4",
                                     "password": "x"})
    resp = auth_client.post(f"/m/automation/{wf_id}/ai-flags",
                            json={"ai_lock": False})
    assert resp.status_code == 404


def _add_ai_conn(client):
    return client.post("/settings/ai/add", data={
        "name": "Test Conn", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test", "model": "gpt-4o-mini", "vision_model": "",
        "is_active": "on"}, follow_redirects=True)


def _chat_capture(auth_client, payload):
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


def test_chat_open_workflow_context(auth_client, app, user, enabled):
    """Opening a workflow in the editor and chatting: the agent gets a system
    note identifying the workflow (regression: module_service import)."""
    _add_ai_conn(auth_client)
    wf_id = _workflow(app, user, _text_graph(), name="Flow on screen")
    messages = _chat_capture(auth_client, {
        "message": "add a node to this workflow",
        "context_workflow_id": wf_id})
    notes = [m for m in messages
             if m["role"] == "system" and "automation workflow" in m["content"]]
    assert notes
    assert f"[id {wf_id}]" in notes[0]["content"]
    assert "Flow on screen" in notes[0]["content"]


def test_chat_hidden_workflow_adds_no_context_note(
        auth_client, app, user, enabled):
    """AI-hidden workflows stay invisible even as the open-editor context."""
    _add_ai_conn(auth_client)
    wf_id = _workflow(app, user, _text_graph(), ai_hidden=True)
    messages = _chat_capture(auth_client, {
        "message": "hi", "context_workflow_id": wf_id})
    assert not [m for m in messages
                if m["role"] == "system"
                and "automation workflow" in m["content"]]
