"""Dashboards module: document model, attachments, versioning (Task 2)."""

import importlib.util
import io
import json
import os
import sys

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import AIUsageEvent, Drive, ModuleState, StoredFile, User
from app.services import file_service, indexing_service

# Load the module package directly (it lives outside the app package).
_DASH_DIR = os.path.join(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "modules", "dashboards"))
_spec = importlib.util.spec_from_file_location(
    "docindex_module_dashboards", os.path.join(_DASH_DIR, "__init__.py"),
    submodule_search_locations=[_DASH_DIR])
_dash = importlib.util.module_from_spec(_spec)

sys.modules.setdefault("docindex_module_dashboards", _dash)
_spec.loader.exec_module(_dash)
dash_doc = importlib.import_module("docindex_module_dashboards.doc")


@pytest.fixture()
def enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="dashboards", enabled=True))
        db.session.commit()


def _drive(app, user_id, synced=False):
    with app.app_context():
        d = Drive(name="Personal", user_id=user_id,
                  source_path="/tmp/real" if synced else None)
        db.session.add(d)
        db.session.commit()
        return d.id


def _create(app, user_id, drive_id, title="Morning view"):
    with app.app_context():
        user = db.session.get(User, user_id)
        drive = db.session.get(Drive, drive_id)
        stored = dash_doc.create_dashboard(user, drive, title)
        indexing_service.index_file(stored.id, app)  # tests run inline
        return stored.id


def _upload(filename="data.csv", content=b"a,b\n1,2\n"):
    return FileStorage(stream=io.BytesIO(content), filename=filename,
                       content_type="text/csv")


# ---------------------------------------------------------------------------
# Card validation
# ---------------------------------------------------------------------------

def test_card_validation_rejects_unknown_type():
    with pytest.raises(ValueError):
        dash_doc.new_card("nope")
    with pytest.raises(ValueError):
        dash_doc.new_card(type="nope")
    with pytest.raises(ValueError):
        dash_doc.validate({"version": 1, "title": "T",
                           "cards": [{"type": "nope"}],
                           "attachments": [], "createdFileIds": []})


def test_new_card_accepts_type_keyword():
    assert dash_doc.new_card(type="kpi")["type"] == "kpi"


def test_validate_snaps_sizes_to_grid():
    doc = dash_doc.empty_dashboard("Grid")
    tiny = dash_doc.new_card("chart")
    tiny["w"], tiny["h"] = 1, 1  # below the chart minimum
    wide = dash_doc.new_card("kpi")
    wide["w"], wide["h"] = 50, 99  # above the grid
    doc["cards"] = [tiny, wide]
    validated = dash_doc.validate(doc)
    chart, kpi = validated["cards"]
    assert chart["w"] >= 4 and chart["h"] >= 3  # chart minima enforced
    assert 1 <= kpi["w"] <= 12  # clamped to the 12-column grid
    assert kpi["h"] >= 1


def test_validate_rejects_overlong_prompt():
    doc = dash_doc.empty_dashboard("T")
    card = dash_doc.new_card("markdown")
    card["prompt"] = "x" * (dash_doc.PROMPT_MAX_CHARS + 1)
    doc["cards"] = [card]
    with pytest.raises(ValueError):
        dash_doc.validate(doc)


def test_validate_defaults_missing_status_to_idle():
    validated = dash_doc.validate({"version": 1, "title": "T",
                                   "cards": [{"type": "kpi"}],
                                   "attachments": [],
                                   "createdFileIds": []})
    assert validated["cards"][0]["status"] == "idle"


def test_validate_rejects_card_ref_to_unattached_file():
    doc = dash_doc.empty_dashboard("T")
    card = dash_doc.new_card("table")
    card["inputs"] = {"attachments": ["deadbeefcafe"]}
    doc["cards"] = [card]
    with pytest.raises(ValueError):
        dash_doc.validate(doc)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def test_attach_upload_stores_in_dashboard_folder(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        att = dash_doc.attach_upload(stored, _upload())
        assert att["origin"] == "upload"
        asset = db.session.get(StoredFile, att["fileId"])
        assert asset.drive_id == stored.drive_id
        assert asset.folder_id == stored.folder_id
        reloaded = dash_doc.load(db.session.get(StoredFile, fid))
        assert any(a["id"] == att["id"] for a in reloaded["attachments"])


def test_attach_link_dedupes(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        target = dash_doc.create_dashboard(user_obj, drive, "Source")
        target_id = target.id
        stored = db.session.get(StoredFile, fid)
        first = dash_doc.attach_link(stored, target_id)
        second = dash_doc.attach_link(stored, target_id)
        assert first["id"] == second["id"]
        assert first["origin"] == "link"
        reloaded = dash_doc.load(db.session.get(StoredFile, fid))
        assert len([a for a in reloaded["attachments"]
                    if a["fileId"] == target_id]) == 1


def test_validate_rejects_foreign_file_ref(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()
        bob_drive = Drive(name="Bob drive", user_id=bob.id)
        db.session.add(bob_drive)
        db.session.commit()
        foreign = dash_doc.create_dashboard(bob, bob_drive, "Bob's")
        foreign_id = foreign.id
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        with pytest.raises(ValueError):
            dash_doc.attach_link(stored, foreign_id)


def test_detach_upload_removes_file(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        att = dash_doc.attach_upload(stored, _upload())
        result = dash_doc.detach_attachment(stored, att["id"])
        assert result == {"removed_file": True}
        asset = db.session.get(StoredFile, att["fileId"])
        assert asset.deleted_at is not None  # trashed, blob kept
        reloaded = dash_doc.load(db.session.get(StoredFile, fid))
        assert reloaded["attachments"] == []


def test_detach_link_keeps_file(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        target = dash_doc.create_dashboard(user_obj, drive, "Source")
        stored = db.session.get(StoredFile, fid)
        att = dash_doc.attach_link(stored, target.id)
        result = dash_doc.detach_attachment(stored, att["id"])
        assert result == {"removed_file": False}
        assert db.session.get(StoredFile, target.id).deleted_at is None


# ---------------------------------------------------------------------------
# Versioning: snapshot on structure, write-through on results
# ---------------------------------------------------------------------------

def test_save_snapshots_structure_not_results(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["cards"].append(dash_doc.new_card("kpi"))
        assert dash_doc.save_document(
            stored, doc, force_checkpoint=True) is True
        assert len(stored.versions) == 1

        # Result-only write: persisted, but no new snapshot.
        results = dash_doc.load(stored)
        results["cards"][0]["result"] = {"text": "3 open incidents"}
        results["cards"][0]["status"] = "done"
        assert dash_doc.save_document(stored, results) is False
        assert len(stored.versions) == 1
        assert dash_doc.load(stored)["cards"][0]["result"] == {
            "text": "3 open incidents"}

        # Structural edit with checkpoint: new snapshot.
        structural = dash_doc.load(stored)
        structural["cards"][0]["title"] = "Renamed"
        assert dash_doc.save_document(
            stored, structural, force_checkpoint=True) is True
        assert len(stored.versions) == 2


# ---------------------------------------------------------------------------
# FTS extract
# ---------------------------------------------------------------------------

def test_extract_flattens_titles_prompts_results(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, title="Morning view")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        card = dash_doc.new_card("markdown", title="Summary",
                                 prompt="Summarize incidents")
        card["result"] = {"text": "All quiet today"}
        doc["cards"].append(card)
        dash_doc.save_document(stored, doc, force_checkpoint=True)
        dash_doc.attach_upload(stored, _upload("incidents.csv"))
        text = dash_doc.extract(dash_doc.load(stored))
    assert "Morning view" in text
    assert "Summary" in text and "Summarize incidents" in text
    assert "All quiet today" in text
    assert "incidents.csv" in text


# ---------------------------------------------------------------------------
# Module scaffold
# ---------------------------------------------------------------------------

def test_dashboard_routes_require_enabled_module(app, auth_client, user):
    drive_id = _drive(app, user)
    _create(app, user, drive_id)
    # Disabled by default -> guard 404s.
    assert auth_client.get("/m/dashboards/").status_code == 404
    with app.app_context():
        db.session.add(ModuleState(name="dashboards", enabled=True))
        db.session.commit()
    assert auth_client.get("/m/dashboards/").status_code == 200


# ---------------------------------------------------------------------------
# Routes: CRUD, save, history, attachments (Task 3)
# ---------------------------------------------------------------------------

def _create_via_route(auth_client, name="Morning view"):
    resp = auth_client.post("/m/dashboards/new", data={"name": name},
                            follow_redirects=False)
    assert resp.status_code == 302
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def test_save_roundtrip(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create_via_route(auth_client)
    with app.app_context():
        assert db.session.get(StoredFile, fid).drive_id == drive_id

    resp = auth_client.get(f"/m/dashboards/{fid}/doc")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True and data["checksum"]

    doc = data["doc"]
    doc["cards"].append(dash_doc.new_card(
        "kpi", title="Open incidents", prompt="Count open incidents"))
    save = auth_client.post(f"/m/dashboards/{fid}/save", json={"doc": doc})
    assert save.status_code == 200
    payload = save.get_json()
    assert payload["ok"] is True
    assert payload["saved_at"] and payload["checksum"]

    again = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()
    assert again["doc"]["cards"][0]["title"] == "Open incidents"
    assert again["checksum"] == payload["checksum"]


def test_hostile_layout_rejected(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    before = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]

    # Unknown card type.
    bad_type = dict(before, cards=[{"type": "nope"}])
    # Off-grid width: not a grid number at all -> validate() raises.
    # (Plain out-of-range numbers are snapped to the grid by design —
    # see test_validate_snaps_sizes_to_grid — so hostility here means
    # a non-numeric width.)
    bad_width = dict(before, cards=[{**dash_doc.new_card("kpi"),
                                    "w": "huge"}])
    # Card referencing an attachment that is not on the dashboard.
    bad_ref = dict(before, cards=[{**dash_doc.new_card("table"),
                                  "inputs": {"attachments": ["deadbeef"]}}])
    for bad in (bad_type, bad_width, bad_ref):
        resp = auth_client.post(f"/m/dashboards/{fid}/save",
                               json={"doc": bad})
        assert resp.status_code == 400

    # Foreign file id: linking a file that does not exist / is not owned.
    resp = auth_client.post(f"/m/dashboards/{fid}/attachments",
                           json={"file_id": 999999999})
    assert resp.status_code == 400

    # Nothing persisted: doc identical, no versions snapshotted.
    after = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    assert after == before
    with app.app_context():
        assert db.session.get(StoredFile, fid).versions == []


def test_history_versions_on_structural_save(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["cards"].append(dash_doc.new_card("kpi", title="One"))
        assert dash_doc.save_document(
            stored, doc, force_checkpoint=True) is True
        version_id = stored.versions[0].id

    resp = auth_client.get(f"/m/dashboards/{fid}/history")
    assert resp.status_code == 200
    diff = auth_client.get(
        f"/m/dashboards/{fid}/history/{version_id}/diff")
    assert diff.status_code == 200
    assert b"One" in diff.data

    # A structural save through the route snapshots a new version.
    doc2 = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    doc2["cards"].append(dash_doc.new_card("table", title="Two"))
    save = auth_client.post(f"/m/dashboards/{fid}/save",
                           json={"doc": doc2, "force_checkpoint": True})
    assert save.status_code == 200
    with app.app_context():
        assert len(db.session.get(StoredFile, fid).versions) == 2

    # Restore brings back the snapshotted (empty-cards) content.
    back = auth_client.post(
        f"/m/dashboards/{fid}/history/{version_id}/restore",
        follow_redirects=True)
    assert back.status_code == 200
    with app.app_context():
        assert dash_doc.load(
            db.session.get(StoredFile, fid))["cards"] == []


def test_attachments_upload_link_and_detach(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)

    # Upload: stored in the dashboard's drive/folder, dashboard-owned.
    resp = auth_client.post(
        f"/m/dashboards/{fid}/attachments",
        data={"file": (io.BytesIO(b"a,b\n1,2\n"), "data.csv")},
        content_type="multipart/form-data")
    assert resp.status_code == 200
    uploaded = resp.get_json()["attachment"]
    assert uploaded["origin"] == "upload"

    # Link: reference to another owned file (dedupes on repeat).
    target_id = _create(app, user, drive_id, "Source")
    first = auth_client.post(f"/m/dashboards/{fid}/attachments",
                            json={"file_id": target_id}).get_json()
    second = auth_client.post(f"/m/dashboards/{fid}/attachments",
                             json={"file_id": target_id}).get_json()
    assert first["attachment"]["id"] == second["attachment"]["id"]

    doc = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    assert len(doc["attachments"]) == 2

    # Detach the upload: the owned file is trashed, the entry is gone.
    gone = auth_client.delete(
        f"/m/dashboards/{fid}/attachments/{uploaded['id']}")
    assert gone.status_code == 200
    assert gone.get_json()["removed_file"] is True
    with app.app_context():
        asset = db.session.get(StoredFile, uploaded["fileId"])
        assert asset.deleted_at is not None
        assert len(dash_doc.load(
            db.session.get(StoredFile, fid))["attachments"]) == 1


def test_editor_shell_boots_spa_safe(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    resp = auth_client.get(f"/m/dashboards/{fid}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "DashboardEditor" in body
    for key in ("dashboardId", "saveUrl", "docUrl", "runUrl", "statusUrl"):
        assert key in body
    assert "?v=1" in body


def test_new_rejects_unknown_location(app, auth_client, user, enabled):
    # Explicit drive:/folder: ids that do not exist -> 404, nothing created
    # (must NOT fall through to the default-drive fallback).
    drive_id = _drive(app, user)
    with app.app_context():
        before = StoredFile.query.filter_by(
            user_id=user, extension="pdash").count()
    for data in ({"name": "Lost", "location": "drive:999999"},
                 {"name": "Lost", "location": "folder:999999"},
                 {"name": "Lost", "drive_id": str(drive_id),
                  "folder_id": "999999"}):
        assert auth_client.post("/m/dashboards/new",
                               data=data).status_code == 404
    with app.app_context():
        assert StoredFile.query.filter_by(
            user_id=user, extension="pdash").count() == before


def test_new_rejects_malformed_location(app, auth_client, user, enabled):
    # Malformed ids / schemes -> 400 (never a 500), nothing created.
    _drive(app, user)
    for data in ({"name": "X", "location": "drive:abc"},
                 {"name": "X", "location": "folder:abc"},
                 {"name": "X", "location": "bogus"},
                 {"name": "X", "drive_id": "abc"},
                 {"name": "X", "folder_id": "1.5x"}):
        assert auth_client.post("/m/dashboards/new",
                               data=data).status_code == 400
    with app.app_context():
        assert StoredFile.query.filter_by(
            user_id=user, extension="pdash").count() == 0


# ---------------------------------------------------------------------------
# Task 4: execution engine (queue, run_card/run_all, cleanup)
# ---------------------------------------------------------------------------

def _engine():
    """The execution engine module (Tasks 4+) — ImportError until implemented."""
    return importlib.import_module("docindex_module_dashboards.engine")


def _automation_models():
    name = "docindex_module_automation"
    if sys.modules.get(name) is None:
        auto_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "modules", "automation")
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(auto_dir, "__init__.py"),
            submodule_search_locations=[auto_dir])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return importlib.import_module(f"{name}.models")


@pytest.fixture()
def fake_ai(monkeypatch):
    """Stub the engine's AI seams: no real backend calls in tests."""
    dash_engine = _engine()
    calls = {}

    def fake_agent(user, prompt, drive=None):
        calls["agent_prompt"] = prompt
        return ("3 open incidents",
                [{"prompt_tokens": 10, "completion_tokens": 5,
                  "estimated": True, "model": "fake"}])

    def fake_complete(user, messages):
        calls["messages"] = messages
        return ("formatted summary",
                {"prompt_tokens": 8, "completion_tokens": 4,
                 "estimated": True, "model": "fake"})

    monkeypatch.setattr(dash_engine, "run_agent_answer", fake_agent)
    monkeypatch.setattr(dash_engine, "complete_once", fake_complete)
    return calls


def _add_card(app, fid, type="kpi", title="Card", prompt="Say hi", tools=True,
              attachments=(), workflow=None):
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        card = dash_doc.new_card(type, title=title, prompt=prompt)
        card["tools"] = tools
        card["inputs"]["attachments"] = list(attachments)
        if workflow is not None:
            card["inputs"]["workflow"] = workflow
        doc["cards"].append(card)
        dash_doc.save_document(stored, doc, force_checkpoint=True)
        return card["id"]


def _attach_file(app, fid, name="data.csv", content=b"a,b\n1,2\n"):
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        return dash_doc.attach_upload(stored, _upload(name, content))


def _set_running(app, fid, card_id):
    """Mark a card running directly (simulates an in-flight run)."""
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        for card in doc["cards"]:
            if card["id"] == card_id:
                card["status"] = "running"
                card["runId"] = f"{fid}:deadbeef"
        dash_doc.save_document(stored, doc)


def _card(app, fid, card_id):
    with app.app_context():
        doc = dash_doc.load(db.session.get(StoredFile, fid))
        return next(c for c in doc["cards"] if c["id"] == card_id)


def test_run_card_snapshots_result(app, auth_client, user, enabled, fake_ai):
    dash_engine = _engine()
    assert dash_engine  # bound (fails before the engine exists)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid)
    cid = _add_card(app, fid, type="kpi", title="Open",
                    prompt="Count open", attachments=[att["id"]])
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid})
    assert resp.status_code == 200
    run_id = resp.get_json()["accepted"][0]
    assert "Count open" in fake_ai["agent_prompt"]

    st = auth_client.get(f"/m/dashboards/runs/{run_id}/status")
    assert st.status_code == 200
    body = st.get_json()
    assert body["status"] == "done"
    assert body["result"]["text"] == "3 open incidents"

    card = _card(app, fid, cid)
    assert card["status"] == "done"
    assert card["result"] == {"text": "3 open incidents"}
    assert card["ranAt"]
    with app.app_context():
        assert (db.session.query(AIUsageEvent)
                .filter_by(user_id=user, kind="dashboard").count() >= 1)


def test_run_all_runs_every_card(app, auth_client, user, enabled, fake_ai):
    _engine()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    first = _add_card(app, fid, title="One")
    second = _add_card(app, fid, title="Two")
    resp = auth_client.post(f"/m/dashboards/{fid}/run", json={"all": True})
    assert resp.status_code == 200
    assert len(resp.get_json()["accepted"]) == 2
    assert _card(app, fid, first)["status"] == "done"
    assert _card(app, fid, second)["status"] == "done"


def test_refresh_while_running_rejected(app, auth_client, user, enabled,
                                        fake_ai):
    _engine()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid)
    _set_running(app, fid, cid)
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid})
    assert resp.status_code == 409
    body = resp.get_json()
    assert cid in body["already_running"]
    assert body["error"]  # human-readable notice
    # Single active run: nothing executed, state untouched.
    assert "agent_prompt" not in fake_ai
    card = _card(app, fid, cid)
    assert card["status"] == "running" and card["result"] is None


def test_missing_input_fails_visibly(app, auth_client, user, enabled,
                                     fake_ai):
    _engine()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid, name="gone.csv")
    cid = _add_card(app, fid, prompt="Count rows",
                    attachments=[att["id"]])
    with app.app_context():
        file_service.delete_file(db.session.get(StoredFile, att["fileId"]))
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid})
    assert resp.status_code == 200
    run_id = resp.get_json()["accepted"][0]
    body = auth_client.get(
        f"/m/dashboards/runs/{run_id}/status").get_json()
    assert body["status"] == "error"
    assert "gone.csv" in body["error"]
    card = _card(app, fid, cid)
    assert card["status"] == "error"
    assert "gone.csv" in card["result"]["error"]


def test_tools_off_truncates_huge_file(app, auth_client, user, enabled,
                                        fake_ai):
    dash_engine = _engine()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    big = ("0123456789\n" * 6000).encode()
    att = _attach_file(app, fid, name="big.csv", content=big)
    cid = _add_card(app, fid, tools=False, prompt="Summarize",
                    attachments=[att["id"]])
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid})
    assert resp.status_code == 200
    run_id = resp.get_json()["accepted"][0]
    body = auth_client.get(
        f"/m/dashboards/runs/{run_id}/status").get_json()
    assert body["status"] == "done"
    assert body["result"]["truncated"] is True
    injected = fake_ai["messages"][0]["content"]
    assert len(big) > dash_engine.TOOLS_OFF_MAX_CHARS
    assert (len(injected) <= dash_engine.TOOLS_OFF_MAX_CHARS
            + len("Summarize") + 2000)


def test_interrupted_on_load(app, auth_client, user, enabled):
    _engine()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid)
    _set_running(app, fid, cid)
    doc = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    card = next(c for c in doc["cards"] if c["id"] == cid)
    assert card["status"] == "interrupted"
    assert "interrupt" in card["result"]["error"].lower()


def test_clear_artifacts_keeps_dashboard_and_attachments(
        app, auth_client, user, enabled):
    dash_engine = _engine()
    assert dash_engine
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        drive = db.session.get(Drive, drive_id)
        owner = db.session.get(User, user)
        art = file_service.save_upload(_upload("artifact.csv"), owner,
                                       drive=drive)
        art_id = art.id
        orig = file_service.save_upload(_upload("orig.csv"), owner,
                                        drive=drive)
        orig_id = orig.id
        stored = db.session.get(StoredFile, fid)
        dash_doc.register_artifact(stored, art_id)
        dash_doc.attach_link(stored, orig_id)
    uploaded = _attach_file(app, fid, name="owned.csv")

    resp = auth_client.post(f"/m/dashboards/{fid}/artifacts/clear")
    assert resp.status_code == 200
    assert resp.get_json()["cleared"] == [art_id]
    with app.app_context():
        assert db.session.get(StoredFile, art_id).deleted_at is not None
        assert db.session.get(StoredFile, uploaded["fileId"]
                              ).deleted_at is None
        assert db.session.get(StoredFile, orig_id).deleted_at is None
        doc = dash_doc.load(db.session.get(StoredFile, fid))
        assert doc["createdFileIds"] == []
        assert len(doc["attachments"]) == 2


def test_delete_removes_dashboard_owned_uploads_and_artifacts_but_keeps_linked_originals(
        app, auth_client, user, enabled):
    dash_engine = _engine()
    assert dash_engine
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        drive = db.session.get(Drive, drive_id)
        owner = db.session.get(User, user)
        art = file_service.save_upload(_upload("artifact.csv"), owner,
                                       drive=drive)
        art_id = art.id
        orig = file_service.save_upload(_upload("orig.csv"), owner,
                                        drive=drive)
        orig_id = orig.id
        stored = db.session.get(StoredFile, fid)
        dash_doc.register_artifact(stored, art_id)
        dash_doc.attach_link(stored, orig_id)
    uploaded = _attach_file(app, fid, name="owned.csv")

    info = auth_client.get(f"/m/dashboards/{fid}/delete-info").get_json()
    assert info["dashboard"]["id"] == fid
    assert uploaded["fileId"] in [a["id"] for a in info["ownedUploads"]]
    assert art_id in [a["id"] for a in info["artifacts"]]
    assert orig_id in [a["id"] for a in info["linkedKept"]]

    resp = auth_client.post(f"/m/dashboards/{fid}/delete")
    assert resp.status_code == 200
    assert auth_client.get(f"/m/dashboards/{fid}/doc").status_code == 404
    with app.app_context():
        assert db.session.get(StoredFile, fid).deleted_at is not None
        assert (db.session.get(StoredFile, uploaded["fileId"])
                .deleted_at is not None)
        assert db.session.get(StoredFile, art_id).deleted_at is not None
        assert db.session.get(StoredFile, orig_id).deleted_at is None


def test_run_workflow_card_uses_runtime(app, auth_client, user, enabled,
                                        fake_ai):
    _engine()
    auto_models = _automation_models()
    graph = {"nodes": [
        {"id": "n1", "type": "input.text", "x": 0, "y": 0,
         "config": {"content": "hello world", "format": "text"}},
        {"id": "n2", "type": "output.result", "x": 0, "y": 0, "config": {}},
    ], "edges": [
        {"from": "n1", "from_port": "out", "to": "n2", "to_port": "in"}]}
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        wf = auto_models.AutomationWorkflow(
            user_id=user, name="Flow", graph=json.dumps(graph))
        db.session.add(wf)
        db.session.commit()
        wid = wf.id
    cid = _add_card(app, fid, type="workflow", prompt="Summarize",
                    workflow={"workflow_id": wid, "params": {}})
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid})
    assert resp.status_code == 200
    run_id = resp.get_json()["accepted"][0]
    body = auth_client.get(
        f"/m/dashboards/runs/{run_id}/status").get_json()
    assert body["status"] == "done"
    previews = [r["preview"]
                for r in body["result"]["workflow"]["results"]]
    assert any("hello world" in p for p in previews)
    assert body["result"]["text"] == "formatted summary"
