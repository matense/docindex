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


def _foreign_file(app, user):
    """A file owned by another user; returns its id."""
    with app.app_context():
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()
        bob_drive = Drive(name="Bob drive", user_id=bob.id)
        db.session.add(bob_drive)
        db.session.commit()
        foreign = dash_doc.create_dashboard(bob, bob_drive, "Bob's")
        return foreign.id


def test_save_rejects_forged_foreign_attachment(app, auth_client, user,
                                                enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    foreign_id = _foreign_file(app, user)
    before = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]

    # Forged attachment pointing at another user's file: 400, never
    # persisted (same standard as the attach_link endpoint).
    forged = dict(before, attachments=[
        {"id": "forge00000001", "fileId": foreign_id, "name": "stolen",
         "origin": "link", "addedAt": "2026-01-01T00:00:00"}])
    resp = auth_client.post(f"/m/dashboards/{fid}/save",
                           json={"doc": forged})
    assert resp.status_code == 400

    # Forged attachment pointing at a nonexistent file: same 400.
    forged_missing = dict(before, attachments=[
        {"id": "forge00000002", "fileId": 999999999, "name": "ghost",
         "origin": "link", "addedAt": "2026-01-01T00:00:00"}])
    assert auth_client.post(f"/m/dashboards/{fid}/save",
                           json={"doc": forged_missing}).status_code == 400

    after = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    assert after == before
    assert after["attachments"] == []
    with app.app_context():
        assert db.session.get(StoredFile, foreign_id).deleted_at is None
        assert db.session.get(StoredFile, fid).versions == []


def test_save_strips_forged_created_file_ids(app, auth_client, user,
                                             enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    foreign_id = _foreign_file(app, user)
    before = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]

    # Forged artifact registry entry (foreign id) is neutralized: stripped
    # at save time so no cross-user effect is possible.
    forged = dict(before, createdFileIds=[foreign_id, 999999999])
    resp = auth_client.post(f"/m/dashboards/{fid}/save",
                           json={"doc": forged})
    assert resp.status_code == 200
    after = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    assert after["createdFileIds"] == []

    # The foreign file is untouched and never surfaces as an artifact.
    info = auth_client.get(
        f"/m/dashboards/{fid}/delete-info").get_json()
    assert info["artifacts"] == []
    with app.app_context():
        assert db.session.get(StoredFile, foreign_id).deleted_at is None


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


# ---------------------------------------------------------------------------
# Task 7: AI tools (dashboards.*)
# ---------------------------------------------------------------------------

def _dash_tools():
    """The AI tools module (Task 7+) — ImportError until implemented."""
    return importlib.import_module("docindex_module_dashboards.tools")


def _workflow_id(app, user):
    auto_models = _automation_models()
    with app.app_context():
        graph = {"nodes": [
            {"id": "n1", "type": "input.text", "x": 0, "y": 0,
             "config": {"content": "hello world", "format": "text"}},
            {"id": "n2", "type": "output.result", "x": 0, "y": 0,
             "config": {}},
        ], "edges": [
            {"from": "n1", "from_port": "out", "to": "n2",
             "to_port": "in"}]}
        wf = auto_models.AutomationWorkflow(
            user_id=user, name="Flow", graph=json.dumps(graph))
        db.session.add(wf)
        db.session.commit()
        return wf.id


def test_ai_create_builds_dashboard(app, user, enabled):
    tools = _dash_tools()
    wid = _workflow_id(app, user)
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = tools._tool_create(
            user_obj,
            {"name": "Morning view",
             "cards": [
                 {"type": "kpi", "title": "Open",
                  "prompt": "Count open incidents"},
                 {"type": "workflow", "title": "Flow",
                  "prompt": "Summarize the flow output",
                  "workflow": {"workflow_id": wid, "params": {}}}]},
            None)
        assert "created" in result, result
        stored = db.session.get(StoredFile, result["created"]["id"])
        doc = dash_doc.load(stored)
        assert [c["type"] for c in doc["cards"]] == ["kpi", "workflow"]
        assert (doc["cards"][1]["inputs"]["workflow"]["workflow_id"]
                == wid)
        # Unknown card types are rejected and create nothing.
        bad = tools._tool_create(
            user_obj, {"name": "Bad", "cards": [{"type": "nope"}]},
            None)
        assert "error" in bad and "created" not in bad


def test_ai_attach_and_card_ref(app, user, enabled):
    tools = _dash_tools()
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        fid = tools._tool_create(
            user_obj, {"name": "Dash"}, None)["created"]["id"]
        target = tools._tool_create(
            user_obj, {"name": "Source"}, None)["created"]["id"]
        attached = tools._tool_attach_file(
            user_obj, {"file_id": fid, "target_file_id": target}, None)
        assert "attached" in attached, attached
        att_id = attached["attached"]["id"]
        ok = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "table",
                       "title": "Rows", "prompt": "List rows",
                       "attachments": [att_id]}, None)
        assert ok["ok"] is True, ok
        # A file id that is not attached to the dashboard is rejected...
        foreign = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "table",
                       "attachments": [str(target)]}, None)
        assert "error" in foreign
        # ... as is a made-up attachment id.
        ghost = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "table",
                       "attachments": ["deadbeefcafe"]}, None)
        assert "error" in ghost
        doc = dash_doc.load(db.session.get(StoredFile, fid))
        assert len(doc["cards"]) == 1
        assert doc["cards"][0]["inputs"]["attachments"] == [att_id]


def test_ai_run_card_single_history_block(app, user, enabled, fake_ai):
    tools = _dash_tools()
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        fid = tools._tool_create(
            user_obj, {"name": "Live",
                       "cards": [{"type": "kpi", "title": "Open",
                                  "prompt": "Count open"}]},
            None)["created"]["id"]
        attached = tools._tool_attach_file(
            user_obj, {"file_id": fid, "name": "data.csv",
                       "content": "a,b\n1,2\n"}, None)
        assert "attached" in attached, attached
        added = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "table", "title": "Rows",
                       "prompt": "List rows",
                       "attachments": [attached["attached"]["id"]]},
            None)
        assert added["ok"] is True, added
        ran = tools._tool_run_card(
            user_obj, {"file_id": fid, "card_id": added["card"]["id"]},
            None)
        assert ran["status"] == "done", ran
        assert ran["result"]["text"] == "3 open incidents"
        stored = db.session.get(StoredFile, fid)
        assert dash_doc.load(stored)["cards"][1]["status"] == "done"
        # One AI answer = one restorable history block.
        assert len(stored.versions) == 1
        assert stored.versions[0].source == "ai"


def test_ai_recommend_proposes_card_edits(app, user, enabled):
    tools = _dash_tools()
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        fid = tools._tool_create(
            user_obj, {"name": "Empty"}, None)["created"]["id"]
        recs = tools._tool_recommend(user_obj, {"file_id": fid}, None)
        assert recs["recommendations"], recs
        assert any("proposed_card" in r for r in recs["recommendations"])
        tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "kpi"}, None)
        recs2 = tools._tool_recommend(user_obj, {"file_id": fid}, None)
        assert any(r.get("card_id") for r in recs2["recommendations"])


# ---------------------------------------------------------------------------
# Task 8: attach-via-tree browser modal (JSON browse endpoints)
# ---------------------------------------------------------------------------

def _seed_browse_tree(app, user, drive_id):
    """Drive root: folder 'Proj' > subfolder 'Sub' + root file; nested file in
    'Sub'; one trashed file at root (must stay invisible). Returns ids."""
    from app.models import Folder
    with app.app_context():
        owner = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        proj = Folder(name="Proj", user_id=user, drive_id=drive_id)
        db.session.add(proj)
        db.session.commit()
        sub = Folder(name="Sub", user_id=user, drive_id=drive_id,
                     parent_id=proj.id)
        db.session.add(sub)
        db.session.commit()
        root_file = file_service.save_upload(
            _upload("root.csv"), owner, None, drive=drive)
        nested = file_service.save_upload(
            _upload("nested.csv"), owner, sub, drive=drive)
        gone = file_service.save_upload(
            _upload("gone.csv"), owner, None, drive=drive)
        # save_upload leaves drive_id unset (the drive upload route assigns
        # it afterwards) — mirror that so the seed matches real rows.
        root_file.drive_id = drive_id
        nested.drive_id = drive_id
        gone.drive_id = drive_id
        db.session.commit()
        gone_id, root_id, nested_id = gone.id, root_file.id, nested.id
        proj_id, sub_id = proj.id, sub.id
        file_service.delete_file(gone)
        return {"proj": proj_id, "sub": sub_id, "root": root_id,
                "nested": nested_id, "gone": gone_id}


def test_browse_requires_own_drive(app, auth_client, user, enabled):
    from app.models import Drive, Folder
    drive_id = _drive(app, user)
    with app.app_context():
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()
        bob_drive = Drive(name="Bob drive", user_id=bob.id)
        db.session.add(bob_drive)
        db.session.commit()
        foreign_drive = bob_drive.id
        foreign_folder = Folder(name="Bob folder", user_id=bob.id,
                                drive_id=foreign_drive)
        db.session.add(foreign_folder)
        db.session.commit()
        foreign_folder_id = foreign_folder.id

    # Own drives only.
    drives = auth_client.get("/m/dashboards/browse/drives")
    assert drives.status_code == 200
    own_ids = [d["id"] for d in drives.get_json()]
    assert drive_id in own_ids
    assert foreign_drive not in own_ids

    # Foreign drive id -> 404.
    assert auth_client.get(
        f"/m/dashboards/browse/children?drive_id={foreign_drive}"
    ).status_code == 404
    # parent_id from another drive (even with a valid own drive_id) -> 404.
    assert auth_client.get(
        "/m/dashboards/browse/children"
        f"?drive_id={drive_id}&parent_id={foreign_folder_id}"
    ).status_code == 404


def test_browse_lists_folders_and_files(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    ids = _seed_browse_tree(app, user, drive_id)

    root = auth_client.get(
        f"/m/dashboards/browse/children?drive_id={drive_id}")
    assert root.status_code == 200
    body = root.get_json()
    assert [f["name"] for f in body["folders"]] == ["Proj"]
    proj = body["folders"][0]
    assert proj["id"] == ids["proj"] and proj["hasKids"] is True
    assert [f["name"] for f in body["files"]] == ["root.csv"]
    entry = body["files"][0]
    assert entry["id"] == ids["root"]
    assert entry["mime"] and entry["size"] > 0
    assert "gone.csv" not in [f["name"] for f in body["files"]]

    mid = auth_client.get(
        "/m/dashboards/browse/children"
        f"?drive_id={drive_id}&parent_id={ids['proj']}")
    assert mid.status_code == 200
    mid_body = mid.get_json()
    assert [f["name"] for f in mid_body["folders"]] == ["Sub"]
    assert mid_body["folders"][0]["hasKids"] is True
    assert mid_body["files"] == []

    leaf = auth_client.get(
        "/m/dashboards/browse/children"
        f"?drive_id={drive_id}&parent_id={ids['sub']}")
    assert leaf.status_code == 200
    leaf_body = leaf.get_json()
    assert leaf_body["folders"] == []
    assert [f["name"] for f in leaf_body["files"]] == ["nested.csv"]


def test_browse_search_matches_names(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    with app.app_context():
        owner = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        file_service.save_upload(
            _upload("quarterly-report.csv"), owner, None, drive=drive)
        file_service.save_upload(
            _upload("unrelated.txt"), owner, None, drive=drive)
        for i in range(25):
            file_service.save_upload(
                _upload(f"cap-{i:02d}.txt"), owner, None, drive=drive)
        db.session.commit()

    hit = auth_client.get("/m/dashboards/browse/search?q=quarterly")
    assert hit.status_code == 200
    names = [f["name"] for f in hit.get_json()["files"]]
    assert "quarterly-report.csv" in names
    assert "unrelated.txt" not in names

    miss = auth_client.get("/m/dashboards/browse/search?q=zzz-no-such-file")
    assert miss.status_code == 200
    assert miss.get_json()["files"] == []

    capped = auth_client.get("/m/dashboards/browse/search?q=cap-")
    assert capped.status_code == 200
    assert len(capped.get_json()["files"]) == 20

    assert auth_client.get(
        "/m/dashboards/browse/search?q=").status_code == 400
    assert auth_client.get("/m/dashboards/browse/search").status_code == 400


def test_browse_js_calls_module_root_urls():
    """Regression (controller-verified in the browser): the tree modal must
    call ``/m/dashboards/browse/*`` (module root), never
    ``/m/dashboards/<id>/browse/*`` — the per-dashboard base 404s there, so
    the modal showed "Falhou a carregar as drives" on every open."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert 'S.base + "/browse/' not in src
    for path in ("/browse/drives", "/browse/children", "/browse/search"):
        assert ('moduleBase + "%s' % path) in src


def test_attach_merges_server_attachment_before_save():
    """Regression (controller-verified in the browser): after POST
    /attachments succeeded, the client called ``save(S, false)`` with the
    STALE ``S.doc`` — wiping the just-added attachment server-side, so the
    doc showed ``attachments: []`` despite POST returning ok:true. The
    client must merge the returned entry into ``S.doc`` (dedupe by id)
    before saving, so concurrent local edits (e.g. an unsaved prompt
    change) survive too; detach must drop the entry locally for the same
    reason (else save resurrects it)."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    merge = re.search(r"function mergeAttached\(S, att\) \{(.*?)\n    \}",
                      src, re.S)
    drop = re.search(r"function dropAttached\(S, attId\) \{(.*?)\n    \}",
                     src, re.S)
    assert merge, "mergeAttached helper missing"
    assert drop, "dropAttached helper missing"
    # Both attach flows (multipart upload + browse-link) merge before save;
    # detach drops before save.
    assert src.count("mergeAttached(S, out.data.attachment)") >= 2
    assert "dropAttached(S, attId)" in src
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (merge.group(0) + "\n" + drop.group(0) + """
var S = {doc: {cards: [{id: "c1", prompt: "local edit"}],
    attachments: [{id: "a1"}]}};
mergeAttached(S, {id: "a2", name: "novo.csv"});
var keptEdit = S.doc.cards[0].prompt === "local edit";
var merged = S.doc.attachments.length === 2;
mergeAttached(S, {id: "a2", name: "novo.csv"});
var deduped = S.doc.attachments.length === 2;
mergeAttached(S, null);
var nullSafe = S.doc.attachments.length === 2;
dropAttached(S, "a1");
var dropped = S.doc.attachments.length === 1 &&
    S.doc.attachments[0].id === "a2";
console.log(JSON.stringify([keptEdit, merged, deduped, nullSafe, dropped]));
""")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == [True] * 5


def test_browse_rows_indent_by_shared_depth_and_truncate():
    """Regression (screenshot-verified): tree file rows rendered at root
    indent while folders nested, and long names forced a horizontal
    scrollbar. The depth→padding mapping lives in the DOM-less
    ``browseDepthPad`` helper (evaluated here with node); folder heads,
    leaves and file rows must all build their indent from it, and names
    must ellipsize inside an overflow-x-hidden tree."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(
        r"function browseDepthPad\(depth\) \{(.*?)\n    \}", src, re.S)
    assert match, "browseDepthPad helper missing"
    # Both row builders share the helper: folder heads/leaves and file rows.
    assert src.count("browseDepthPad(depth)") >= 2
    assert "dash-bname" in src
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (match.group(0) + ";console.log("
             "[browseDepthPad(0),browseDepthPad(1),"
             "browseDepthPad(2),browseDepthPad(3)].join(','))")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    pads = [int(v) for v in out.stdout.strip().split(",")]
    assert pads == [8, 26, 44, 62]
    assert all(b > a for a, b in zip(pads, pads[1:]))
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    assert "overflow-x: hidden" in css
    assert ".dash-bname" in css
    for prop in ("text-overflow: ellipsis", "white-space: nowrap",
                 "min-width: 0"):
        assert prop in css
