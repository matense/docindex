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


def test_kpi_cards_allow_compact_3x1():
    """Task 11: KPI cards can shrink to 3x1 — a compact single-value row
    taking less grid space when the user wants it."""
    doc = dash_doc.empty_dashboard("Compact")
    slim = dash_doc.new_card("kpi")
    slim["w"], slim["h"] = 3, 1
    doc["cards"] = [slim]
    validated = dash_doc.validate(doc)
    assert validated["cards"][0]["w"] == 3
    assert validated["cards"][0]["h"] == 1


def test_builder_allows_kpi_height_1():
    """Task 11: the Builder's per-type height floor lets KPI cards reach
    h=1 (pointer resize, keyboard resize and new-card defaults all read
    the same MIN_H table)."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"var MIN_H = (\{[^}]*\});", src)
    assert match, "MIN_H table missing"
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = ("var MIN_H = " + match.group(1) + ";"
             "console.log([MIN_H.kpi, MIN_H.table, MIN_H.chart].join(','))")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().split(",") == ["1", "2", "3"]


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
    assert "?v=14" in body
    assert "?v=34" in body


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

    def fake_agent(user, prompt, drive=None, max_steps=None):
        calls["agent_prompt"] = prompt
        calls["max_steps"] = max_steps
        return ("3 open incidents",
                [{"prompt_tokens": 10, "completion_tokens": 5,
                  "estimated": True, "model": "fake"}],
                [])

    def fake_complete(user, messages):
        calls["messages"] = messages
        return ("formatted summary",
                {"prompt_tokens": 8, "completion_tokens": 4,
                 "estimated": True, "model": "fake"})

    monkeypatch.setattr(dash_engine, "run_agent_collected", fake_agent)
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


def test_browse_excludes_trashed_folders(app, auth_client, user, enabled):
    """Trashed folders stay out of the tree: invisible in children, a
    parent whose only content is trashed reports hasKids False, a trashed
    parent 404s, and files inside trashed folders stay out of search."""
    from app.models import Folder, utcnow
    drive_id = _drive(app, user)
    ids = _seed_browse_tree(app, user, drive_id)
    with app.app_context():
        db.session.get(Folder, ids["sub"]).deleted_at = utcnow()
        db.session.commit()

    # "Sub" trashed: gone from the mid level, "Proj" left childless.
    mid = auth_client.get(
        "/m/dashboards/browse/children"
        f"?drive_id={drive_id}&parent_id={ids['proj']}")
    assert mid.status_code == 200
    assert mid.get_json()["folders"] == []
    root = auth_client.get(
        f"/m/dashboards/browse/children?drive_id={drive_id}")
    proj = root.get_json()["folders"][0]
    assert proj["id"] == ids["proj"] and proj["hasKids"] is False

    # A trashed parent is not browsable.
    assert auth_client.get(
        "/m/dashboards/browse/children"
        f"?drive_id={drive_id}&parent_id={ids['sub']}").status_code == 404

    # The file itself is healthy, but its folder is trashed:
    # quick search must not surface it.
    search = auth_client.get("/m/dashboards/browse/search?q=nested")
    assert search.status_code == 200
    assert search.get_json()["files"] == []

    # Trash "Proj" too: root level hides it and refuses it as a parent.
    with app.app_context():
        db.session.get(Folder, ids["proj"]).deleted_at = utcnow()
        db.session.commit()
    root = auth_client.get(
        f"/m/dashboards/browse/children?drive_id={drive_id}")
    assert root.get_json()["folders"] == []
    assert auth_client.get(
        "/m/dashboards/browse/children"
        f"?drive_id={drive_id}&parent_id={ids['proj']}").status_code == 404


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
        assert ("moduleBase + \"" + path) in src


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
    assert all(b > a for a, b in zip(pads, pads[1:], strict=False))
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    assert "overflow-x: hidden" in css
    assert ".dash-bname" in css
    for prop in ("text-overflow: ellipsis", "white-space: nowrap",
                 "min-width: 0"):
        assert prop in css


# ---------------------------------------------------------------------------
# Task 9: Builder polish — prompt textareas auto-expand, full English UI
# ---------------------------------------------------------------------------

def test_dashboard_ui_ships_in_english():
    """Task 9 (user scope): the dashboards module UI is English — no
    Portuguese diacritics in the shipped JS/templates, and the key
    Builder/View chrome strings read English."""
    import re
    files = [os.path.join(_DASH_DIR, "static", "dashboard.js"),
             os.path.join(_DASH_DIR, "templates", "dashboards", "list.html"),
             os.path.join(_DASH_DIR, "templates", "dashboards", "edit.html"),
             os.path.join(_DASH_DIR, "templates", "dashboards",
                           "history.html"),
             os.path.join(_DASH_DIR, "templates", "dashboards", "diff.html")]
    pt = re.compile("[ãõçéêáàâîôûäëïöüñÃÕÇÉÊÁÀÂÎÔÛ]")
    bad = []
    for path in files:
        with open(path, encoding="utf-8") as fh:
            import unicodedata
            src = unicodedata.normalize("NFC", fh.read())
            for num, line in enumerate(src.splitlines(), 1):
                if pt.search(line):
                    bad.append(f"{os.path.basename(path)}:{num}: {line.strip()[:100]}")
    assert not bad, ("Portuguese UI strings remain:\n" +
                     "\n".join(bad[:25]))
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        js = fh.read()
    for s in ("Add card", "Card prompt", "Run all", "Delete dashboard",
              "No attachments", "Attach existing file", "Run this card",
              "No content", "Upload file", "Clear artifacts"):
        assert s in js, f"missing English chrome string: {s!r}"


def test_prompt_textareas_autogrow():
    """Task 9 (user scope): card prompt textareas grow with their content
    instead of clipping at a fixed height. Task C2 removed the manual
    workflow-params textarea (params now go through the contract form),
    so the growth wiring lives on the prompt textarea + the picker."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"function autoGrow\(el\) \{(.*?)\n    \}", src, re.S)
    assert match, "autoGrow helper missing"
    # Wired to the card prompt textarea (input + post-append fit).
    assert src.count("autoGrow(") >= 3
    assert "dash-prompt" in src and "dash-wf-select" in src
    assert "dash-wf-params" not in src  # C2: manual JSON textarea gone
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (match.group(0) +
             ";var el={style:{},scrollHeight:132};autoGrow(el);"
             "console.log(el.style.height)")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "132px"
    # Initial fit must run post-append: a detached textarea reports
    # scrollHeight 0, so fitting inside buildCard (pre-append) no-ops and
    # clips long prompts (overflow-y hidden). renderGrid fits each card's
    # visible textareas after inserting it into the document.
    grid = re.search(r"function renderGrid\(S\) \{(.*?)\n    \}", src, re.S)
    assert grid and "autoGrow(" in grid.group(1)


# ---------------------------------------------------------------------------
# Task 10: structured card outputs (per-type contracts + result parsing)
# ---------------------------------------------------------------------------

def _stub_ai(monkeypatch, answer):
    """Stub the engine's agent seam with a canned answer; capture prompt."""
    dash_engine = _engine()
    calls = {}

    def fake_agent(user, prompt, drive=None, max_steps=None):
        calls["agent_prompt"] = prompt
        calls["max_steps"] = max_steps
        return (answer, [{"prompt_tokens": 1, "completion_tokens": 1,
                          "estimated": True, "model": "fake"}],
                [])

    monkeypatch.setattr(dash_engine, "run_agent_collected", fake_agent)
    return calls


def _run_and_finish(app, auth_client, fid, cid):
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid})
    assert resp.status_code == 200
    run_id = resp.get_json()["accepted"][0]
    st = auth_client.get(f"/m/dashboards/runs/{run_id}/status")
    assert st.status_code == 200
    body = st.get_json()
    assert body["status"] == "done"
    return body["result"]


def test_run_card_kpi_parses_structured_json(app, auth_client, user,
                                             enabled, monkeypatch):
    """Task 10: a KPI card whose answer carries JSON (+file-list garbage)
    snapshots a clean {"kpi": {...}} — the View renders one big value,
    not the raw prose (screenshot: numbered text + leaked file refs)."""
    answer = ('```json\n{"value": "177,8 kWh", "label": "jun/2026"}\n```\n'
              '(Ficheiros usados: [entrada.csv] (file://1/110888))')
    calls = _stub_ai(monkeypatch, answer)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid)
    cid = _add_card(app, fid, type="kpi", title="Low month",
                    prompt="Lowest month and value",
                    attachments=[att["id"]])
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"kpi": {"value": "177,8 kWh", "label": "jun/2026"}}
    assert "file://" not in str(result)
    assert "JSON" in calls["agent_prompt"]
    assert _card(app, fid, cid)["result"] == result


def test_run_card_kpi_falls_back_to_text(app, auth_client, user,
                                         enabled, monkeypatch):
    """Task 10: KPI prose with no JSON keeps the old {"text"} behavior."""
    _stub_ai(monkeypatch, "3 open incidents")
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Open",
                    prompt="Count open")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"text": "3 open incidents"}


def test_run_card_chart_parses_and_validates(app, auth_client, user,
                                             enabled, monkeypatch):
    """Task 10: chart JSON is validated like notebook chart cells — bad
    kind defaults to bar, labels/data capped, numeric coercion applied."""
    import json
    spec = {"chart": {"kind": "donut", "labels": [f"m{i}" for i in
                                                 range(60)],
                      "datasets": [{"label": "kWh",
                                    "data": ["10", "x", 30.5]}]}}
    calls = _stub_ai(monkeypatch, json.dumps(spec))
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="chart", title="Usage",
                    prompt="Monthly usage chart")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert set(result) == {"chart"}
    chart = result["chart"]
    assert chart["kind"] == "bar"
    assert len(chart["labels"]) == 50
    assert chart["datasets"] == [{"label": "kWh", "data": [10, 0, 30.5]}]
    assert "JSON" in calls["agent_prompt"]


def test_run_card_chart_falls_back_to_text(app, auth_client, user,
                                           enabled, monkeypatch):
    """Task 10: a chart answer with no usable spec keeps {"text"}."""
    _stub_ai(monkeypatch, "no chart here, just prose")
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="chart", title="Usage",
                    prompt="Monthly usage chart")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"text": "no chart here, just prose"}


def test_run_card_chart_malformed_shapes_never_error(app, auth_client,
                                                   user, enabled,
                                                   monkeypatch):
    """Task 10 fix round: malformed model output (huge ints, non-list
    labels/data) must fall back to a snapshot — never an error state."""
    import json
    huge = {"chart": {"kind": "bar", "labels": ["a", "b"],
                      "datasets": [{"label": "x",
                                    "data": [10 ** 400, 5]}]}}
    _stub_ai(monkeypatch, json.dumps(huge))
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="chart", title="Usage",
                    prompt="Monthly usage chart")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert set(result) == {"chart"}
    assert result["chart"]["datasets"][0]["data"] == [0, 5]

    _stub_ai(monkeypatch, json.dumps({"chart": {"kind": "bar",
                                                "labels": 5,
                                                "datasets": "nope"}}))
    cid2 = _add_card(app, fid, type="chart", title="Usage 2",
                     prompt="Monthly usage chart")
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid2})
    assert resp.status_code == 200
    run_id = resp.get_json()["accepted"][0]
    st = auth_client.get(f"/m/dashboards/runs/{run_id}/status")
    assert st.status_code == 200
    assert st.get_json()["status"] == "done"


def test_run_card_list_contract_bans_file_refs(app, auth_client, user,
                                               enabled, monkeypatch):
    """Task 10: list/table prompts carry the no-file-refs contract; their
    answers still snapshot as text (renderer line-splits lists)."""
    calls = _stub_ai(monkeypatch, "- alpha\n- beta")
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="list", title="Tops",
                    prompt="Top items")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"text": "- alpha\n- beta"}
    assert "file://" in calls["agent_prompt"].lower() or \
        "file references" in calls["agent_prompt"]


# ---------------------------------------------------------------------------
# Run-console plan — Task 1: per-card step budget
# ---------------------------------------------------------------------------

def test_max_steps_validated_and_default_none():
    card = dash_doc.new_card("kpi")
    assert card["maxSteps"] is None
    card["maxSteps"] = 5
    assert dash_doc.validate({**dash_doc.empty_dashboard("T"),
                              "cards": [card]})["cards"][0]["maxSteps"] == 5
    for bad in (0, -3, 129, 2.5, True, "many", [4]):
        card["maxSteps"] = bad
        with pytest.raises(ValueError):
            dash_doc.validate({**dash_doc.empty_dashboard("T"),
                               "cards": [card]})


def test_run_passes_max_steps_to_agent(app, auth_client, user, enabled,
                                       monkeypatch):
    dash_engine = _engine()
    seen = {}

    def fake_collected(user, prompt, drive=None, max_steps=None):
        seen["max_steps"] = max_steps
        return ("done-text", [], [])

    monkeypatch.setattr(dash_engine, "run_agent_collected", fake_collected)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", prompt="Hi")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["cards"][0]["maxSteps"] = 7
        dash_doc.save_document(stored, doc, force_checkpoint=True)
    assert auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid}).status_code == 200
    assert seen["max_steps"] == 7


def test_tools_off_ignores_max_steps(app, auth_client, user, enabled,
                                     monkeypatch):
    calls = {}

    def fake_complete(user, messages):
        calls["body"] = messages[0]["content"]
        return ("formatted summary",
                {"prompt_tokens": 8, "completion_tokens": 4,
                 "estimated": True, "model": "fake"})

    monkeypatch.setattr(_engine(), "complete_once", fake_complete)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", prompt="Hi", tools=False)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["cards"][0]["maxSteps"] = 7
        dash_doc.save_document(stored, doc, force_checkpoint=True)
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"text": "formatted summary"}
    assert "Output contract" in calls["body"]


# ---------------------------------------------------------------------------
# Run-console plan — Task 2: run trace collection
# ---------------------------------------------------------------------------

def test_run_collects_step_trace(app, auth_client, user, enabled,
                                 monkeypatch):
    def fake_events(user, history, drive=None, **kw):
        yield ("tool_start", {"label": "read file", "name": "read",
                              "index": 1, "max_steps": 7})
        yield ("step", {"label": "read file", "detail": "opened a.csv",
                        "index": 1, "max_steps": 7})
        yield ("thinking", "need the june rows")
        yield ("answer", "final")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_events)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="list", prompt="Tops")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"text": "final"}
    trace = _card(app, fid, cid)["trace"]
    assert trace[0] == {"index": 1, "label": "read file", "detail": "read"}
    assert trace[1] == {"index": 1, "label": "read file",
                        "detail": "opened a.csv"}
    assert any(e["label"] == "thinking" for e in trace)


def test_trace_caps_and_old_docs(app, auth_client, user, enabled,
                                 monkeypatch):
    def fake_many(user, history, drive=None, **kw):
        for i in range(1, 201):
            yield ("step", {"label": "s", "detail": "d",
                            "index": i, "max_steps": 128})
        yield ("answer", "final")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_many)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="list", prompt="Tops")
    _run_and_finish(app, auth_client, fid, cid)
    assert len(_card(app, fid, cid)["trace"]) <= 128
    doc = dash_doc.empty_dashboard("Old")
    doc["cards"] = [{"id": "c1", "type": "kpi", "title": "T",
                     "w": 4, "h": 2, "prompt": "Hi"}]
    validated = dash_doc.validate(doc)
    assert validated["cards"][0]["maxSteps"] is None
    assert validated["cards"][0]["trace"] is None


# ---------------------------------------------------------------------------
# Run duration: how long each card took, shown on the card
# ---------------------------------------------------------------------------

def test_run_records_duration(app, auth_client, user, enabled, fake_ai):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="T", prompt="Hi")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"text": "3 open incidents"}
    card = _card(app, fid, cid)
    assert isinstance(card.get("ranSecs"), (int, float))
    assert card["ranSecs"] >= 0


def test_stream_run_records_duration(app, auth_client, user, enabled,
                                     monkeypatch):
    def fake_events(user, history, drive=None, **kw):
        yield ("step", {"label": "read", "detail": "a.csv",
                        "index": 1, "max_steps": 7})
        yield ("answer", "final")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_events)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="list", prompt="Tops")
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    b"".join(resp.response)
    card = _card(app, fid, cid)
    assert isinstance(card.get("ranSecs"), (int, float))
    assert card["ranSecs"] >= 0


def test_duration_format_units():
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"function fmtDur\(s\) \{(.*?)\n    \}", src, re.S)
    assert match, "fmtDur helper missing"
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (match.group(0) +
             ";console.log([fmtDur(4.2), fmtDur(75), fmtDur(3725)]"
             ".join('|'))")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "4s|1m 15s|1h 2m"


def test_ai_tool_run_records_duration(app, user, enabled, monkeypatch):
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="T", prompt="Hi")
    monkeypatch.setattr(tools.engine_service, "run_card",
                        lambda stored, card, uid: ({"text": "w"}, [], []))
    with app.app_context():
        user_obj = db.session.get(User, user)
        out = tools._tool_run_card(user_obj, {"file_id": fid,
                                              "card_id": cid}, None)
    assert out.get("status") == "done"
    assert out.get("status") == "done"
    assert isinstance(_card(app, fid, cid).get("ranSecs"), (int, float))


# ---------------------------------------------------------------------------
# Live-run registry: reloads must not kill live runs
# ---------------------------------------------------------------------------

def test_recover_skips_live_runs(app, user):
    """A card marked running WITH a live worker registered must survive
    recover_interrupted (reloading/navigating mid-run must not fake an
    interruption); a running card with no live worker is still recovered."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        live = dash_doc.new_card("kpi", title="Live")
        live["status"] = "running"
        live["runId"] = f"{fid}:live123"
        dead = dash_doc.new_card("kpi", title="Dead")
        dead["status"] = "running"
        dead["runId"] = f"{fid}:dead456"
        doc["cards"] = [live, dead]
        dash_doc.save_document(stored, doc, force_checkpoint=True)
        dash_doc.run_started(fid, f"{fid}:live123")
        try:
            assert dash_doc.recover_interrupted(stored) == 1
        finally:
            dash_doc.run_finished(fid, f"{fid}:live123")
        reloaded = dash_doc.load(db.session.get(StoredFile, fid))
        by_title = {c["title"]: c for c in reloaded["cards"]}
        assert by_title["Live"]["status"] == "running"
        assert "error" not in (by_title["Live"].get("result") or {})
        assert by_title["Dead"]["status"] == "interrupted"
        assert "interrupted" in by_title["Dead"]["result"]["error"]


def test_live_registry_tracks_runs():
    assert not dash_doc.is_run_live(1, "1:abc")
    dash_doc.run_started(1, "1:abc")
    try:
        assert dash_doc.is_run_live(1, "1:abc")
        assert not dash_doc.is_run_live(1, "1:other")
        assert not dash_doc.is_run_live(2, "1:abc")
    finally:
        dash_doc.run_finished(1, "1:abc")
    assert not dash_doc.is_run_live(1, "1:abc")
    dash_doc.run_finished(1, "1:never")  # unknown: no crash


def test_help_documents_prose_fallback():
    """The help page carries the honest note: if the model answers prose
    instead of JSON the card shows text by design — re-run the card."""
    import json
    with open(os.path.join(_DASH_DIR, "module.json"),
                 encoding="utf-8") as fh:
        module = json.load(fh)
    blob = "\n".join(module.get("help", []))
    assert "plain text" in blob
    assert "run" in blob.lower() and "again" in blob.lower()


# ---------------------------------------------------------------------------
# Prose-plus-JSON answers (screenshot: model writes prose, JSON last)
# ---------------------------------------------------------------------------

def test_kpi_parses_trailing_json_after_prose(app, auth_client, user,
                                              enabled, monkeypatch):
    answer = ("All three files read. Minimum is jun/2026 with 17737.23 Wh.\n"
              '{"value": "177,8 kWh", "label": "jun/2026"}')
    _stub_ai(monkeypatch, answer)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Low month",
                    prompt="Lowest month and value")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"kpi": {"value": "177,8 kWh", "label": "jun/2026"}}


def test_chart_parses_trailing_json_after_prose(app, auth_client, user,
                                               enabled, monkeypatch):
    import json
    spec = {"chart": {"kind": "bar", "title": "Consumo mensal (kWh)",
                      "labels": ["dez/2025", "jan/2026"],
                      "datasets": [{"label": "kWh", "data": [370.9, 362.9]}]}}
    answer = ("Merging the sections by date, rounded to 1 decimal.\n"
              + json.dumps(spec))
    _stub_ai(monkeypatch, answer)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="chart", title="Usage",
                    prompt="Monthly usage chart")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"chart": spec["chart"]}


def test_braces_in_prose_without_json_fall_back(app, auth_client, user,
                                               enabled, monkeypatch):
    _stub_ai(monkeypatch, "Use {a, b} notation for {x} here, no JSON at all")
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Tops", prompt="Tops")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"text": "Use {a, b} notation for {x} here, no JSON at all"}


# ---------------------------------------------------------------------------
# Live-console plan — fix round (review findings)
# ---------------------------------------------------------------------------

def test_stream_honors_tools_off(app, auth_client, user, enabled,
                                 monkeypatch):
    """Fix: tools-off cards via /run/stream must use the single LLM call,
    never the agent loop."""
    dash_engine = _engine()
    calls = {}

    def fake_complete(user, messages):
        calls["body"] = messages[0]["content"]
        return ("formatted summary",
                {"prompt_tokens": 8, "completion_tokens": 4,
                 "estimated": True, "model": "fake"})

    def boom_events(user, history, drive=None, **kw):
        raise AssertionError("agent loop must not run for tools-off")

    monkeypatch.setattr(dash_engine, "complete_once", fake_complete)
    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        boom_events)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", prompt="Hi", tools=False)
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    body = b"".join(resp.response).decode()
    assert '"t": "done"' in body
    assert "formatted summary" in body
    assert _card(app, fid, cid)["status"] == "done"


def test_iter_run_events_rejects_empty_prompt(app, user, enabled):
    dash_engine = _engine()
    with pytest.raises(ValueError):
        list(dash_engine.iter_run_events(
            None,
            {"id": "c1", "type": "list", "prompt": "  ",
             "tools": True, "inputs": {"attachments": []}}, None))


def test_stream_disconnect_still_settles(app, auth_client, user, enabled,
                                        monkeypatch):
    """Fix: closing the stream mid-run must settle the card (done), never
    leave it stuck running."""
    def fake_events(user, history, drive=None, **kw):
        yield ("step", {"label": "read", "detail": "a.csv",
                        "index": 1, "max_steps": 7})
        yield ("answer", "final")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_events)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="list", prompt="Tops")
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    first = b""
    for chunk in resp.response:
        first += chunk
        if b'"t": "step"' in first:
            break
    assert b'"t": "step"' in first
    resp.response.close()
    card = _card(app, fid, cid)
    assert card["status"] == "done"
    assert card["result"] == {"text": "final"}


def test_stream_drain_error_settles_error(app, auth_client, user, enabled,
                                         monkeypatch):
    """Fix: an agent failure after partial frames settles error state."""
    def fake_events(user, history, drive=None, **kw):
        yield ("step", {"label": "read", "detail": "a.csv",
                        "index": 1, "max_steps": 7})
        raise ValueError("boom mid-run")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_events)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="list", prompt="Tops")
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    body = b"".join(resp.response).decode()
    assert '"t": "done"' in body
    card = _card(app, fid, cid)
    assert card["status"] == "error"
    assert "boom mid-run" in card["result"]["error"]


def test_stream_workflow_registers_artifacts(app, auth_client, user,
                                            enabled, monkeypatch):
    """Fix: workflow runs via /run/stream must join the artifact registry
    like the classic path."""
    dash_tools = _dash_tools()
    wid = _workflow_id(app, user)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid)
    created_id = att["fileId"]
    cid = _add_card(app, fid, type="workflow", prompt="Summarize",
                    workflow={"workflow_id": wid, "params": {}})
    monkeypatch.setattr(dash_tools.engine_service, "run_card",
                        lambda stored, card, uid: (
                            {"text": "w"}, [created_id], []))
    # C2 streams workflow progress through iter_workflow_events (not a
    # single run_card call), so the stub feeds the new seam.
    def fake_workflow_events(stored, card, run_user):
        yield {"t": "done", "card_id": card.get("id"),
               "result": {"text": "w"}, "trace": [],
               "artifact_ids": [created_id]}
    monkeypatch.setattr(dash_tools.engine_service, "iter_workflow_events",
                        fake_workflow_events)
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    b"".join(resp.response)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert created_id in dash_doc.load(stored)["createdFileIds"]


def test_stream_frames_carry_card_id(app, user, enabled, monkeypatch):
    dash_engine = _engine()

    def fake_events(user, history, drive=None, **kw):
        yield ("step", {"label": "read", "detail": "a.csv",
                        "index": 1, "max_steps": 7})
        yield ("answer", "final")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_events)
    frames = list(dash_engine.iter_run_events(
        None,
        {"id": "c9", "type": "list", "prompt": "Tops",
         "tools": True, "inputs": {"attachments": []}}, None))
    assert frames[0]["card_id"] == "c9"
    assert frames[-1]["card_id"] == "c9"


# ---------------------------------------------------------------------------
# Live-console plan — Task 2: SSE endpoint + live frontend
# ---------------------------------------------------------------------------

def test_run_stream_endpoint_streams_frames(app, auth_client, user,
                                            enabled, monkeypatch):
    dash_engine = _engine()
    if not hasattr(dash_engine, "iter_run_events"):
        pytest.fail("iter_run_events seam missing")

    def fake_events(user, history, drive=None, **kw):
        yield ("step", {"label": "read", "detail": "a.csv",
                        "index": 1, "max_steps": 7})
        yield ("answer", "final")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_events)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="list", prompt="Tops")
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    body = b"".join(resp.response).decode()
    assert body.startswith(": connected")
    assert 'data: {"t": "step"' in body
    assert '"t": "done"' in body
    assert _card(app, fid, cid)["status"] == "done"


def test_view_subscribes_live_stream():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "run/stream" in src
    assert "getReader" in src or "ReadableStream" in src
    assert "frame.card_id" in src


def test_console_opens_only_on_icon_click():
    """The live console must never auto-open on run start: the stream
    buffers frames silently and the popup opens only via the icon."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "openLiveConsole" not in src
    assert "ensureLiveBuffer" in src


def test_console_close_never_cancels_stream():
    """Closing the popup (x / Esc / outside click) only hides it — the
    run continues untouched. Reader cancel belongs to view teardown."""
    import re
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"function closeConsole\(V\) \{(.*?)\n    \}", src,
                      re.S)
    assert match, "closeConsole missing"
    assert "consoleReader" not in match.group(1)
    assert "cancel" not in match.group(1)
    teardown = re.findall(r"root\._dashTeardown = function \(\) \{(.*?)\n    \}",
                         src, re.S)
    assert teardown, "teardown missing"
    assert any("cancelConsoleReaders" in body for body in teardown)


# ---------------------------------------------------------------------------
# Card-size plan — Task 1: honor card height
# ---------------------------------------------------------------------------

def test_card_height_is_honored():
    import re
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    m = re.search(r"\.dash-grid \{(.*?)\}", css, re.S)
    assert m and "align-items" in m.group(1)
    assert "stretch" not in m.group(1).replace("start", "")
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "dash-chart-wrap" in src
    assert re.search(r"card\.h\s*\*", src), \
        "wrap height must derive from card.h"


def test_same_type_rows_stay_aligned():
    """Same type + same h in one row must share the row's max height
    (content varies: 1- vs 2-line labels); different h must stay free."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "equalizeRowHeights" in src


# ---------------------------------------------------------------------------
# Card-size plan — Task 2: forgiving drop targets
# ---------------------------------------------------------------------------

def test_drop_target_looks_through_overlays():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "elementsFromPoint" in src


def test_run_all_has_stop_button():
    """Run-all mode shows a Stop button that stops after in-flight cards."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "stopall" in src
    assert "runAllStop = true" in src


def test_btn_hidden_guard_wins():
    """style.css loads after tailwind and sets .btn{display:inline-flex},
    which beats tailwind's .hidden on equal specificity — any btn that
    relies on .hidden (the Stop button) would never hide. A scoped
    guard must always win."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    assert ".btn.hidden" in css


# ---------------------------------------------------------------------------
# Live-console plan — Task 1: streaming engine seam
# ---------------------------------------------------------------------------

def test_iter_run_events_streams_then_done(app, user, enabled, monkeypatch):
    dash_engine = _engine()
    if not hasattr(dash_engine, "iter_run_events"):
        pytest.fail("iter_run_events seam missing")

    def fake_events(user, history, drive=None, **kw):
        yield ("tool_start", {"label": "read", "name": "r",
                              "index": 1, "max_steps": 7})
        yield ("thinking", {"oops": 1})
        yield ("thinking", "plan the months")
        yield ("answer", "final")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_events)
    frames = list(dash_engine.iter_run_events(
        None,
        {"id": "c1", "type": "list", "prompt": "Tops",
         "tools": True, "inputs": {"attachments": []}}, None))
    kinds = [f["t"] for f in frames]
    assert kinds[0] == "step" and kinds[-1] == "done"
    assert any(f.get("label") == "thinking" for f in frames)
    done = frames[-1]
    assert done["result"] == {"text": "final"}
    assert isinstance(done["trace"], list)


def test_iter_run_events_no_steps_still_done(app, user, enabled,
                                            monkeypatch):
    dash_engine = _engine()
    if not hasattr(dash_engine, "iter_run_events"):
        pytest.fail("iter_run_events seam missing")

    def fake_instant(user, history, drive=None, **kw):
        yield ("answer", "42")

    monkeypatch.setattr("app.services.agent_service.run_agent_events",
                        fake_instant)
    frames = list(dash_engine.iter_run_events(
        None,
        {"id": "c1", "type": "kpi", "prompt": "Hi",
         "tools": True, "inputs": {"attachments": []}}, None))
    assert frames[-1]["t"] == "done"
    assert frames[-1]["result"] == {"text": "42"}


# ---------------------------------------------------------------------------
# Run-console plan — Task 3: run console popup (View)
# ---------------------------------------------------------------------------

def test_view_renders_run_console():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "dash-console-pop" in src
    assert "console-btn" in src
    assert "console-close" in src
    assert "card.trace" in src or 'card["trace"]' in src


def test_popups_hide_despite_display_rules():
    """Popup close bug: component classes that set `display` share
    specificity with tailwind's `.hidden` and dashboard.css loads later,
    so `display:flex/block` used to win and popups never hid. Each such
    popup needs a scoped `.hidden` guard that always wins."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    for cls in ("dash-refpick", "dash-console-pop", "dash-browse-tree"):
        assert ("." + cls + ".hidden" in css), \
            "missing .hidden guard for ." + cls


# ---------------------------------------------------------------------------
# Run-console plan — Task 4: table snapshots
# ---------------------------------------------------------------------------

def test_run_card_table_parses_pipe_rows(app, auth_client, user, enabled,
                                        monkeypatch):
    _stub_ai(monkeypatch, "Month | Wh | kWh | % of total\n"
                          "dez/2025 | 370906.01 | 370.9 | 10.6\n"
                          "jan/2026 | 362908.78 | 362.9")
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="table", title="Usage",
                    prompt="Monthly usage table")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert result == {"table": {
        "headers": ["Month", "Wh", "kWh", "% of total"],
        "rows": [["dez/2025", "370906.01", "370.9", "10.6"],
                 ["jan/2026", "362908.78", "362.9", ""]]}}


def test_run_card_table_caps_and_fallback(app, auth_client, user,
                                         enabled, monkeypatch):
    big = "h1 | h2\n" + "".join("a | b\n" for _ in range(600))
    _stub_ai(monkeypatch, big)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="table", title="Big",
                    prompt="Big table")
    result = _run_and_finish(app, auth_client, fid, cid)
    assert set(result) == {"table"}
    assert len(result["table"]["rows"]) == 500
    _stub_ai(monkeypatch, "no pipes here, just prose")
    cid2 = _add_card(app, fid, type="table", title="Prose",
                     prompt="Prose table")
    assert _run_and_finish(app, auth_client, fid,
                           cid2) == {"text": "no pipes here, just prose"}


# ---------------------------------------------------------------------------
# Attachment-usage plan — Task 1: detach guard + recommend unref
# ---------------------------------------------------------------------------

def test_detach_blocked_while_card_uses_file(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        att = dash_doc.attach_upload(stored, _upload())
        doc = dash_doc.load(stored)
        card = dash_doc.new_card("kpi", title="KPI")
        card["inputs"]["attachments"] = [att["id"]]
        doc["cards"].append(card)
        dash_doc.save_document(stored, doc, force_checkpoint=True)
        with pytest.raises(ValueError, match="KPI"):
            dash_doc.detach_attachment(stored, att["id"])
        reloaded = dash_doc.load(db.session.get(StoredFile, fid))
        assert len(reloaded["attachments"]) == 1  # entry kept
        asset = db.session.get(StoredFile, att["fileId"])
        assert asset.deleted_at is None  # file NOT trashed


def test_detach_blocked_over_route(app, auth_client, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid)
    _add_card(app, fid, attachments=[att["id"]])
    resp = auth_client.delete(
        f"/m/dashboards/{fid}/attachments/{att['id']}")
    assert resp.status_code == 400
    assert "card" in resp.get_json()["error"].lower()


def test_detach_allowed_after_unref(app, user):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        att = dash_doc.attach_upload(stored, _upload())
        doc = dash_doc.load(stored)
        card = dash_doc.new_card("kpi", title="KPI")
        card["inputs"]["attachments"] = [att["id"]]
        doc["cards"].append(card)
        dash_doc.save_document(stored, doc, force_checkpoint=True)
        with pytest.raises(ValueError):
            dash_doc.detach_attachment(stored, att["id"])
        fresh = dash_doc.load(db.session.get(StoredFile, fid))
        fresh["cards"][0]["inputs"]["attachments"] = []
        dash_doc.save_document(stored, fresh, force_checkpoint=True)
        result = dash_doc.detach_attachment(stored, att["id"])
        assert result == {"removed_file": True}
        assert dash_doc.load(
            db.session.get(StoredFile, fid))["attachments"] == []


def test_recommend_suggests_unref_not_detach(app, user, enabled):
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid)
    _add_card(app, fid, attachments=[att["id"]])
    with app.app_context():
        user_obj = db.session.get(User, user)
        target = db.session.get(StoredFile, att["fileId"])
        from app.services import file_service
        file_service.delete_file(target)  # file gone, card ref remains
        recs = tools._tool_recommend(
            user_obj, {"file_id": fid}, None)["recommendations"]
    unrefs = [r for r in recs
              if r.get("kind") == "update_card" and "attachments" in
              r.get("suggested_edit", {})]
    assert unrefs, recs
    assert all("detach_attachment_id" not in r["suggested_edit"]
               for r in recs)


def test_render_attach_with_linked_files():
    """Regression (dashboard 110894 rendered nothing): renderAttach must
    run without throwing when attachments are linked to cards — plain
    JS objects have no .get() (Python-style call broke the whole grid)."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    esc_m = re.search(r"function esc\(value\) \{(.*?)\n    \}", src, re.S)
    refs_m = re.search(r"function cardRefs\(card\) \{(.*?)\n    \}", src,
                       re.S)
    attach_m = re.search(
        r"function renderAttach\(S\) \{(.*?)\n    \}\n", src, re.S)
    assert esc_m and refs_m and attach_m, "helpers missing"
    if shutil.which("node") is None:
        pytest.skip("node not available")
    harness = (
        "function esc(value) {" + esc_m.group(1) + "\n    }\n"
        "function cardRefs(card) {" + refs_m.group(1) + "\n    }\n"
        "function renderAttach(S) {" + attach_m.group(1) + "\n    }\n"
        "var html = '';\n"
        "var listEl = { querySelectorAll: function () { return []; } };\n"
        "Object.defineProperty(listEl, 'innerHTML',\n"
        "  { set: function (v) { html = v; }, get: function () { return html; }, configurable: true });\n"
        "var S = { els: { attachList: listEl }, usageHi: null,\n"
        "  doc: { attachments: [{id: 'a1', name: 'data.csv', origin: 'upload'}],\n"
        "    cards: [{id: 'c1', type: 'kpi', title: 'KPI', inputs: {attachments: ['a1']}}] } };\n"
        "renderAttach(S);\n"
        "if (html.indexOf('data-detach-tip') < 0) throw new Error('no linked tooltip');\n"
        "if (html.indexOf('KPI') < 0) throw new Error('card title missing from tooltip');\n"
        "console.log('RENDER_OK');\n")
    out = subprocess.run(["node", "-e", harness], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "RENDER_OK"


# ---------------------------------------------------------------------------
# Attachment-usage plan — Task 2: usage highlight toggle (Builder)
# ---------------------------------------------------------------------------

def test_attachment_usage_highlight():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "data-usage" in src
    assert "usageHi" in src
    assert "dash-dim" in src and "dash-use-hi" in src
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    assert ".dash-dim" in css and ".dash-use-hi" in css


def test_placeholder_ignores_pointer_events():
    """The drop placeholder must never swallow hit tests: after the first
    swap it sits under the cursor, and every later hover would hit it
    instead of a card — freezing the drag."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    import re
    m = re.search(r"\.dash-ph \{(.*?)\}", css, re.S)
    assert m and "pointer-events" in m.group(1)
    assert "none" in m.group(1)


def test_drop_follows_movement_direction():
    """Drag direction decides before/after (not cursor position inside
    the card): grabbing the top handle pins dy negative, which used to
    lock after=false and made left-to-right moves impossible."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"function dropSide\(mdx, mdy\) \{(.*?)\n    \}",
                      src, re.S)
    assert match, "dropSide helper missing"
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (match.group(0) +
             ";console.log([dropSide(5, 1), dropSide(-5, 1),"
             " dropSide(1, 5), dropSide(1, -5),"
             " dropSide(0, 0)].join(','))")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "true,false,true,false,false"


def test_detach_disabled_tooltip_names_cards():
    """The disabled detach button must explain why via a hoverable
    wrapper (native title doesn't fire on disabled buttons) and name
    the using cards."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "Can\\'t detach" in src or "Cannot detach" in src
    assert "data-detach-tip" in src


def test_ref_change_repaints_attachment_buttons():
    """Fix round: setCardRefs must re-render the attachment list, or the
    detach button keeps a stale disabled state after un/ref."""
    import re
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"function setCardRefs\(S, card, refs\) \{(.*?)\n    \}",
                      src, re.S)
    assert match, "setCardRefs helper missing"
    assert "renderAttach(S)" in match.group(1)


def test_ai_detach_linked_is_refused(app, user, enabled):
    """Review Focus #1 (tools path): the AI detach tool refuses linked
    attachments with the using card(s) named."""
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid)
    _add_card(app, fid, title="KPI", attachments=[att["id"]])
    with app.app_context():
        user_obj = db.session.get(User, user)
        out = tools._tool_detach_file(
            user_obj, {"file_id": fid, "attachment_id": att["id"]},
            None)
    assert "error" in out and "KPI" in out["error"]


# ---------------------------------------------------------------------------
# Argon P0-D3: Builder save vs background results
# ---------------------------------------------------------------------------

def test_builder_save_preserves_background_result(app, auth_client, user,
                                                  enabled, fake_ai):
    """D3: a Builder full-doc save landing after a background run must not
    wipe the run's result/status/ranAt/trace — user structural edits in
    the stale save still apply."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Open", prompt="Count open")
    # Builder snapshot taken BEFORE the background run lands.
    stale = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    assert stale["cards"][0]["result"] is None
    # Background run completes.
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": cid})
    assert resp.status_code == 200
    run_id = resp.get_json()["accepted"][0]
    body = auth_client.get(
        f"/m/dashboards/runs/{run_id}/status").get_json()
    assert body["status"] == "done"
    assert body["result"] == {"text": "3 open incidents"}
    # Builder's older full-doc save (e.g. a debounced autosave in flight).
    stale["cards"][0]["title"] = "Renamed in builder"
    save = auth_client.post(f"/m/dashboards/{fid}/save", json={"doc": stale})
    assert save.status_code == 200
    card = _card(app, fid, cid)
    assert card["result"] == {"text": "3 open incidents"}
    assert card["status"] == "done"
    assert card["ranAt"]
    assert card["title"] == "Renamed in builder"


def test_builder_save_newer_background_result_wins(app, auth_client, user,
                                                   enabled):
    """D3: newer-wins per field — a stale Builder save carrying an older
    completed result must not overwrite the newer background result, its
    trace, or newly registered artifacts; structural edits still apply."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        owner = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        art = file_service.save_upload(_upload("artifact.csv"), owner,
                                       drive=drive)
        art.drive_id = drive_id
        db.session.commit()
        art_id = art.id
    cid = _add_card(app, fid, type="kpi", title="Open", prompt="Count open")
    stale = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    # A newer background run lands server-side (result + trace + artifact).
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        fresh = dash_doc.load(stored)
        server_card = next(c for c in fresh["cards"] if c["id"] == cid)
        server_card["status"] = "done"
        server_card["result"] = {"text": "newer"}
        server_card["trace"] = [{"index": 1, "label": "read",
                                 "detail": "a.csv"}]
        server_card["ranAt"] = "2026-11-16T12:00:00+00:00"
        fresh["createdFileIds"] = [art_id]
        dash_doc.save_document(stored, fresh)
    # The stale Builder save carries an older completed result.
    stale["cards"][0]["status"] = "done"
    stale["cards"][0]["result"] = {"text": "older"}
    stale["cards"][0]["ranAt"] = "2026-01-01T00:00:00+00:00"
    stale["cards"][0]["title"] = "Builder title"
    save = auth_client.post(f"/m/dashboards/{fid}/save", json={"doc": stale})
    assert save.status_code == 200
    card = _card(app, fid, cid)
    assert card["result"] == {"text": "newer"}
    assert card["status"] == "done"
    assert card["ranAt"] == "2026-11-16T12:00:00+00:00"
    assert card["trace"] == [{"index": 1, "label": "read",
                              "detail": "a.csv"}]
    assert card["title"] == "Builder title"
    with app.app_context():
        assert dash_doc.load(
            db.session.get(StoredFile, fid))["createdFileIds"] == [art_id]


def test_builder_save_spares_live_run(app, auth_client, user, enabled):
    """D3: a Builder save landing mid-run must not clear the live running
    state (status/runId) — the status poll and run-status endpoint read it."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Open", prompt="Count open")
    stale = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    run_id = f"{fid}:live123"
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        live = next(c for c in doc["cards"] if c["id"] == cid)
        live["status"] = "running"
        live["runId"] = run_id
        dash_doc.save_document(stored, doc)
        dash_doc.run_started(fid, run_id)
    try:
        save = auth_client.post(f"/m/dashboards/{fid}/save",
                                json={"doc": stale})
        assert save.status_code == 200
    finally:
        dash_doc.run_finished(fid, run_id)
    card = _card(app, fid, cid)
    assert card["status"] == "running"
    assert card["runId"] == run_id


# ---------------------------------------------------------------------------
# Argon P0-D3 fix round 1: reviewer findings 1, 2, 4
# ---------------------------------------------------------------------------

def test_builder_save_both_running_preserves_live_run_id(app, auth_client,
                                                         user, enabled):
    """Fix-1: both sides `running` used to fall through to the `ranAt`
    compare — with both `ranAt` null the stale snapshot's `runId`
    overwrote the live `runId`. Server wins when `runId`s differ."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Open", prompt="Count open")
    stale = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    live_run = f"{fid}:live123"
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        live = next(c for c in doc["cards"] if c["id"] == cid)
        live["status"] = "running"
        live["runId"] = live_run
        dash_doc.save_document(stored, doc)
        dash_doc.run_started(fid, live_run)
    try:
        # Stale snapshot taken during an older run of the same card.
        stale["cards"][0]["status"] = "running"
        stale["cards"][0]["runId"] = f"{fid}:stale999"
        save = auth_client.post(f"/m/dashboards/{fid}/save",
                                json={"doc": stale})
        assert save.status_code == 200
    finally:
        dash_doc.run_finished(fid, live_run)
    card = _card(app, fid, cid)
    assert card["status"] == "running"
    assert card["runId"] == live_run


def test_merge_run_owned_same_run_falls_back_to_ran_at():
    """Fix-1 unit pin: both `running` with the SAME `runId` falls back to
    the `ranAt` compare (newer server run still wins; ties keep incoming,
    which is the same run either way)."""
    base = {"id": "c1", "status": "running", "runId": "9:r1", "ranAt": None,
            "result": None, "trace": None}
    # Same run, nothing newer anywhere: incoming untouched.
    incoming = dict(base, title="local")
    out = dash_doc.merge_run_owned({"cards": [dict(base)], "createdFileIds": []},
                                    {"cards": [incoming], "createdFileIds": []})
    assert out["cards"][0]["runId"] == "9:r1"
    assert out["cards"][0]["title"] == "local"
    # Same run, server ranAt newer (settle raced the snapshot): server wins.
    server = dict(base, status="done", ranAt="2026-11-16T12:00:00+00:00",
                  result={"text": "newer"})
    incoming2 = dict(base, ranAt="2026-01-01T00:00:00+00:00",
                     result={"text": "older"}, title="local")
    out2 = dash_doc.merge_run_owned(
        {"cards": [server], "createdFileIds": []},
        {"cards": [incoming2], "createdFileIds": []})
    assert out2["cards"][0]["result"] == {"text": "newer"}


def test_builder_save_preserves_server_added_card(app, auth_client, user,
                                                  enabled):
    """Fix-2: a card added server-side (AI/another tab) while the Builder
    snapshot was stale must survive the Builder save — union-preserved at
    its live position — while the stale structural edit still applies. The
    Builder sends its base checksum; the mismatch (no run need have
    landed) triggers the union. Without the checksum the save behaves as
    before."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Open", prompt="Count open")
    snap = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()
    stale, base = snap["doc"], snap["checksum"]
    # Concurrent change: a card is added server-side (no run lands).
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        fresh = dash_doc.load(stored)
        fresh["cards"].append(dash_doc.new_card("table", title="AI rows"))
        added_id = fresh["cards"][-1]["id"]
        dash_doc.save_document(stored, fresh)
    stale["cards"][0]["title"] = "Builder title"
    save = auth_client.post(f"/m/dashboards/{fid}/save",
                            json={"doc": stale, "base_checksum": base})
    assert save.status_code == 200
    with app.app_context():
        doc = dash_doc.load(db.session.get(StoredFile, fid))
    assert [c["id"] for c in doc["cards"]] == [cid, added_id]
    assert doc["cards"][0]["title"] == "Builder title"
    assert doc["cards"][1]["title"] == "AI rows"


def test_builder_save_without_checksum_skips_card_union(
        app, auth_client, user, enabled):
    """Fix-2: a save with no base checksum carries no evidence of
    concurrency — membership follows the incoming save (legacy/unknown
    clients behave exactly as before the fix)."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    _add_card(app, fid, type="kpi", title="Open", prompt="Count open")
    stale = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()["doc"]
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        fresh = dash_doc.load(stored)
        fresh["cards"].append(dash_doc.new_card("table", title="AI rows"))
        dash_doc.save_document(stored, fresh)
    save = auth_client.post(f"/m/dashboards/{fid}/save", json={"doc": stale})
    assert save.status_code == 200
    with app.app_context():
        doc = dash_doc.load(db.session.get(StoredFile, fid))
    assert len(doc["cards"]) == 1  # no evidence, no union


def test_builder_save_card_delete_stands_without_concurrent_run(
        app, auth_client, user, enabled):
    """Fix-2 gate pin: with NO concurrent background write (checksums
    match), card membership follows the incoming save — a user card
    deletion is not resurrected. (Union is gated on checksum mismatch;
    unconditional union would break Builder deletes.)"""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    first = _add_card(app, fid, type="kpi", title="Keep",
                      prompt="Count open")
    _add_card(app, fid, type="table", title="Drop", prompt="List rows")
    snap = auth_client.get(f"/m/dashboards/{fid}/doc").get_json()
    stale, base = snap["doc"], snap["checksum"]
    assert len(stale["cards"]) == 2
    # User deletes the second card in the Builder; nothing else changed.
    stale["cards"] = [c for c in stale["cards"] if c["id"] == first]
    stale["cards"][0]["title"] = "Kept renamed"
    save = auth_client.post(f"/m/dashboards/{fid}/save",
                            json={"doc": stale, "base_checksum": base})
    assert save.status_code == 200
    with app.app_context():
        doc = dash_doc.load(db.session.get(StoredFile, fid))
    assert [c["id"] for c in doc["cards"]] == [first]
    assert doc["cards"][0]["title"] == "Kept renamed"


def test_builder_status_poll_behavior():
    """Fix-4: node harness drives the SHIPPED Builder poll wiring with
    fakes — status fetch, run-field merge, footer update, timer teardown,
    and the add/remove scope boundary (no doc fetch for those)."""
    import subprocess
    harness = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dashboard_builder_poll_harness.js")
    proc = subprocess.run(["node", harness], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_builder_merges_run_owned_without_clobbering_local_edits():
    """D3 (live update): the Builder merges remote run-owned fields into
    its local doc — local structural edits survive, newer remote results
    land, and the artifact registry unions."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"function mergeRunOwned\(S, remote\) \{(.*?)\n    \}",
                      src, re.S)
    assert match, "mergeRunOwned helper missing"
    # The Builder subscribes to the card-change channel (aggregate status
    # poll, View precedent) and tears the timer down on unmount.
    assert "startBuilderStatusPoll" in src
    assert re.search(r"function startBuilderStatusPoll\(S\)", src)
    assert "S.opts.statusUrl" in src
    # Fix-2: both save paths send the last-seen checksum so the server can
    # gate the card union on real concurrency evidence.
    for fn in ("save", "flushSync"):
        body = re.search(rf"function {fn}\(S(?:,.*?)?\) \{{(.*?)\n    \}}",
                         src, re.S)
        assert body, f"{fn} helper missing"
        assert "base_checksum" in body.group(1), \
            f"{fn} must send base_checksum"
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (match.group(0) + """
var S = {doc: {cards: [{id: "c1", title: "Local title", prompt: "local edit",
    result: {text: "older"}, status: "done", ranAt: "2026-01-01T00:00:00+00:00",
    trace: null}],
    createdFileIds: []}, checksum: "old"};
var remote = {cards: [{id: "c1", title: "Remote title", prompt: "remote",
    result: {text: "newer"}, status: "done", ranAt: "2026-11-16T12:00:00+00:00",
    trace: [{index: 1}], runId: "9:abc", ranSecs: 1.5}],
    createdFileIds: [41]};
mergeRunOwned(S, remote);
var c = S.doc.cards[0];
var checks = [c.title === "Local title", c.prompt === "local edit",
    JSON.stringify(c.result) === JSON.stringify({text: "newer"}),
    c.status === "done", c.ranAt === "2026-11-16T12:00:00+00:00",
    JSON.stringify(c.trace) === JSON.stringify([{index: 1}]),
    JSON.stringify(S.doc.createdFileIds) === JSON.stringify([41]),
    S.checksum === "old"];
console.log(JSON.stringify(checks));
""")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == [True] * 8


# ---------------------------------------------------------------------------
# Task D5: dashboard_lock released during run_cards execution
# ---------------------------------------------------------------------------

def test_parallel_runcards_do_not_serialize(monkeypatch, tmp_path):
    """D5: two runs admitted under the cap must overlap in the LLM wait —
    the dashboard lock must be released during execution and re-acquired
    per card to settle (run_card_stream shape), with a per-card run-id
    guard so a stale settle never clobbers a newer run."""
    import threading
    import time

    from app import create_app
    from config import TestConfig

    class RaceConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(tmp_path / "race.sqlite")
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False, "timeout": 30}}

    race_app = create_app(RaceConfig)
    with race_app.app_context():
        db.create_all()
        owner = User(username="alice", email="alice@example.com")
        owner.set_password("password123")
        db.session.add(owner)
        db.session.add(ModuleState(name="dashboards", enabled=True))
        db.session.commit()
        uid = owner.id
    drive_id = _drive(race_app, uid)
    fid = _create(race_app, uid, drive_id)
    first = _add_card(race_app, fid, title="One")
    second = _add_card(race_app, fid, title="Two")
    with race_app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["concurrency"] = 2
        dash_doc.save_document(stored, doc)

    entered, exited = {}, {}
    both_in = threading.Event()
    guard = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def fake_run_card(stored, card, user_id):
        entered[card["id"]] = time.monotonic()
        with guard:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        if len(entered) == 2:
            both_in.set()
        both_in.wait(timeout=10)  # serial execution never lets both in
        exited[card["id"]] = time.monotonic()
        with guard:
            state["inflight"] -= 1
        return ({"text": "ok"}, [], [])

    dash_engine = _engine()
    monkeypatch.setattr(dash_engine, "run_card", fake_run_card)

    clients = []
    for _ in range(2):
        client = race_app.test_client()
        assert client.post("/login", data={"username": "alice",
                                           "password": "password123"}
                           ).status_code in (200, 302)
        clients.append(client)

    results, errors = {}, {}

    def hit(slot, client, card_id):
        try:
            results[slot] = client.post(f"/m/dashboards/{fid}/run",
                                        json={"card_id": card_id})
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors[slot] = exc

    threads = [threading.Thread(target=hit, args=(i, clients[i], cid))
               for i, cid in enumerate((first, second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert all(t.is_alive() is False for t in threads)
    assert results[0].status_code == 200, results[0].data
    assert results[1].status_code == 200, results[1].data
    # Both runs were inside the LLM wait at the same time.
    assert len(entered) == 2 and len(exited) == 2
    assert state["peak"] == 2
    assert max(entered.values()) <= min(exited.values())
    # ... and both settled correctly: neither completion clobbered the other.
    for cid in (first, second):
        card = _card(race_app, fid, cid)
        assert card["status"] == "done"
        assert card["result"] == {"text": "ok"}
        assert card["ranAt"]


# ---------------------------------------------------------------------------
# Final-review fix wave: save-vs-settle lock (item 1) + stale run targets
# (item 3). Item 2 (save_document_ai base-unaware) is closed by ruling
# (tracked follow-up P0-FU1) — verified + documented in task-D3-report.md,
# no code change.
# ---------------------------------------------------------------------------

def test_builder_save_races_run_settle_both_survive(monkeypatch, tmp_path):
    """Item 1: Builder save holds dashboard_lock across load-merge-write, so
    a run settling mid-save blocks until the save lands — both survive
    (mirrors D1's third-writer test: the interleaving writer is never
    lost). Pre-fix the settle slips through mid-save and the save wipes
    it (RED)."""
    import threading
    import time

    from app import create_app
    from config import TestConfig

    class RaceConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(tmp_path / "saverace.sqlite")
        SQLALCHEMY_ENGINE_OPTIONS = {
            "connect_args": {"check_same_thread": False, "timeout": 30}}

    race_app = create_app(RaceConfig)
    with race_app.app_context():
        db.create_all()
        owner = User(username="alice", email="alice@example.com")
        owner.set_password("password123")
        db.session.add(owner)
        db.session.add(ModuleState(name="dashboards", enabled=True))
        db.session.commit()
        uid = owner.id
    drive_id = _drive(race_app, uid)
    fid = _create(race_app, uid, drive_id)
    cid = _add_card(race_app, fid, title="One")
    import copy
    with race_app.app_context():
        stored = db.session.get(StoredFile, fid)
        stale_doc = copy.deepcopy(dash_doc.load(stored))
        base_checksum = stored.checksum
    stale_doc["cards"][0]["title"] = "Builder title"

    dash_engine = _engine()
    entered = threading.Event()
    attempted = threading.Event()
    release = threading.Event()
    orig_merge = dash_doc.merge_run_owned

    def blocking_merge(current, incoming, union_cards=False):
        entered.set()
        assert release.wait(timeout=30)
        return orig_merge(current, incoming, union_cards=union_cards)

    monkeypatch.setattr(dash_doc, "merge_run_owned", blocking_merge)

    clients = []
    for _ in range(2):
        client = race_app.test_client()
        assert client.post("/login", data={"username": "alice",
                                           "password": "password123"}
                           ).status_code in (200, 302)
        clients.append(client)

    save_out, settle_out = {}, {}

    def do_save():
        try:
            save_out["resp"] = clients[0].post(
                f"/m/dashboards/{fid}/save",
                json={"doc": stale_doc, "base_checksum": base_checksum})
        except Exception as exc:  # noqa: BLE001 - surfaced below
            save_out["error"] = exc

    def do_settle():
        try:
            attempted.set()
            # The real settle shape (routes.run_cards): lock, fresh load,
            # run-id-guarded write — never clobbers a newer writer.
            with dash_engine.dashboard_lock(fid):
                with race_app.app_context():
                    stored = db.session.get(StoredFile, fid)
                    fresh = dash_doc.load(stored)
                    card = next(c for c in fresh["cards"]
                                if c.get("id") == cid)
                    card["status"] = "done"
                    card["result"] = {"text": "settled"}
                    card["ranAt"] = "2026-11-16T12:00:00+00:00"
                    dash_doc.save_document(stored, fresh)
            settle_out["done"] = True
        except Exception as exc:  # noqa: BLE001 - surfaced below
            settle_out["error"] = exc

    save_thread = threading.Thread(target=do_save)
    save_thread.start()
    try:
        assert entered.wait(timeout=30), "save never reached merge"
        settle_thread = threading.Thread(target=do_settle)
        settle_thread.start()
        assert attempted.wait(timeout=30), "settle never started"
        time.sleep(1.0)
        # The save holds the lock across load-merge-write: the settle must
        # still be blocked (pre-fix it slips through and is wiped below).
        assert settle_out.get("done") is not True, \
            "settle slipped through mid-save (lock not held)"
        assert settle_thread.is_alive(), \
            "settle slipped through mid-save (lock not held)"
        release.set()
        settle_thread.join(timeout=60)
        save_thread.join(timeout=60)
    finally:
        release.set()
    assert not save_thread.is_alive() and not settle_thread.is_alive()
    assert settle_out.get("done") is True, settle_out
    assert "error" not in save_out, save_out
    assert save_out["resp"].status_code == 200, save_out["resp"].data
    card = _card(race_app, fid, cid)
    assert card["title"] == "Builder title"  # save side kept
    assert card["result"] == {"text": "settled"}  # settle side kept
    assert card["status"] == "done"
    assert card["ranAt"] == "2026-11-16T12:00:00+00:00"


def test_run_cards_stale_single_target_returns_404(app, auth_client, user,
                                                   enabled, monkeypatch):
    """Item 3: a card deleted between the pre-lock check and the in-lock
    reload must 404, never StopIteration-500. Pre-fix this raises
    StopIteration (RED)."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    gone = _add_card(app, fid, title="Gone")
    orig_load = dash_doc.load
    calls = {"n": 0}

    def interleaving_load(stored):
        calls["n"] += 1
        if calls["n"] == 2:
            # A concurrent writer deletes the card after the pre-lock
            # check but before the admission reload.
            fresh = orig_load(stored)
            fresh["cards"] = [c for c in fresh["cards"]
                              if c["id"] != gone]
            dash_doc.save_document(stored, fresh)
        return orig_load(stored)

    monkeypatch.setattr(dash_doc, "load", interleaving_load)
    resp = auth_client.post(f"/m/dashboards/{fid}/run",
                            json={"card_id": gone})
    assert resp.status_code == 404
    assert "unknown" in resp.get_json()["error"].lower()


def test_run_cards_stale_all_target_skips_missing(app, auth_client, user,
                                                  enabled, monkeypatch,
                                                  fake_ai):
    """Item 3: run-all with one card deleted mid-admission must not crash
    the whole run — the missing card is skipped, the rest proceed. Pre-fix
    this raises StopIteration (RED)."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    keep = _add_card(app, fid, title="Keep")
    gone = _add_card(app, fid, title="Gone")
    orig_load = dash_doc.load
    calls = {"n": 0}

    def interleaving_load(stored):
        calls["n"] += 1
        if calls["n"] == 2:
            fresh = orig_load(stored)
            fresh["cards"] = [c for c in fresh["cards"]
                              if c["id"] != gone]
            dash_doc.save_document(stored, fresh)
        return orig_load(stored)

    monkeypatch.setattr(dash_doc, "load", interleaving_load)
    resp = auth_client.post(f"/m/dashboards/{fid}/run", json={"all": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert len(body["accepted"]) == 1
    card = _card(app, fid, keep)
    assert card["status"] == "done"
    assert card["result"] == {"text": "3 open incidents"}
    with app.app_context():
        ids = [c["id"] for c in dash_doc.load(
            db.session.get(StoredFile, fid))["cards"]]
    assert gone not in ids


# ---------------------------------------------------------------------------
# Argon P1 B7d: dashboards on_enable/guards, spreadsheet context fallback,
# per-run console reader
# ---------------------------------------------------------------------------

def file_guard_registered(module):
    """Whether an AI file guard is registered for the module."""
    from app.services import agent_service
    return any(m == module for m, _ in agent_service._FILE_GUARDS)


def test_dashboards_have_ai_guards(app, user):
    assert file_guard_registered("dashboards") is True


def test_on_enable_creates_default_drive_once(app, user):
    from app.services import module_service
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert module_service.set_enabled("dashboards", True, user_obj)
        assert module_service.set_enabled("dashboards", True, user_obj)
        drives = Drive.query.filter_by(
            user_id=user, name=_dash.DEFAULT_DRIVE_NAME).all()
        assert len(drives) == 1


def test_hidden_dashboard_invisible_to_core_agent_tools(app, user, enabled):
    from app.services import agent_service
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Invisible")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["ai_hidden"] = True
        dash_doc.save_document(stored, doc, force_checkpoint=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        result = agent_service._tool_read_file(user_obj, fid)
        assert "hidden" in result["error"]
        found = agent_service._tool_search_files(user_obj, "Invisible")
        assert all(r["file_id"] != fid for r in found["results"])


def test_excel_context_falls_back_to_index(app, user):
    import openpyxl
    dash_engine = _engine()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["col_alpha", "col_beta"])
    ws.append([1, 2])
    buf = io.BytesIO()
    wb.save(buf)
    att = _attach_file(app, fid, name="data.xlsx", content=buf.getvalue())
    cid = _add_card(app, fid, prompt="Summarize",
                    attachments=[att["id"]])
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        card = next(c for c in doc["cards"] if c["id"] == cid)
        files = {att["id"]: db.session.get(StoredFile, att["fileId"])}
        text, _truncated = dash_engine.build_context(stored, doc, card,
                                                     files)
    assert text.strip() != ""
    assert "col_alpha" in text  # never ""


def test_console_readers_are_per_run():
    """Concurrent runs must not share one reader: readers are keyed per
    card/run, so a second stream never clobbers the first and teardown
    cancels every live reader."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    setter = re.search(
        r"function setConsoleReader\(V, cardId, reader\) \{(.*?)\n    \}",
        src, re.S)
    clearer = re.search(
        r"function clearConsoleReader\(V, cardId, reader\) \{(.*?)\n    \}",
        src, re.S)
    canceller = re.search(r"function cancelConsoleReaders\(V\) \{(.*?)\n    \}",
                          src, re.S)
    assert setter, "setConsoleReader helper missing"
    assert clearer, "clearConsoleReader helper missing"
    assert canceller, "cancelConsoleReaders helper missing"
    assert "V.consoleReader =" not in src  # no shared scalar anymore
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (setter.group(0) + "\n" + clearer.group(0) + "\n"
             + canceller.group(0) + """
var cancelled = [];
var V = {};
var r1 = { cancel: function () { cancelled.push("c1"); } };
var r2 = { cancel: function () { cancelled.push("c2"); } };
setConsoleReader(V, "c1", r1);
setConsoleReader(V, "c2", r2);
clearConsoleReader(V, "c1", { cancel: function () {} });
var kept = V.consoleReaders["c1"] === r1 &&
    V.consoleReaders["c2"] === r2;
clearConsoleReader(V, "c1", r1);
var cleared = !("c1" in V.consoleReaders) &&
    V.consoleReaders["c2"] === r2;
cancelConsoleReaders(V);
var cancelledBoth = cancelled.join(",") === "c2" &&
    !("c2" in V.consoleReaders);
console.log(JSON.stringify([kept, cleared, cancelledBoth]));
""")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == [True] * 3


def test_ai_lock_blocks_edits_but_allows_reads(app, user, enabled):
    """Fix round (Important 1): a locked dashboard refuses AI mutations
    (cards, attach, runs) with the unlock message, but stays readable and
    listed with the lock visible — notebooks _save_and_report mirror."""
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    _attach_file(app, fid)
    cid = _add_card(app, fid, title="KPI")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["ai_lock"] = True
        dash_doc.save_document(stored, doc, force_checkpoint=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        read = tools._tool_read(user_obj, {"file_id": fid}, None)
        assert "error" not in read
        assert read["ai_lock"] is True
        assert "unlock" in read["note"]
        added = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "kpi", "title": "nope"},
            None)
        assert "locked" in added["error"].lower()
        ran = tools._tool_run_card(
            user_obj, {"file_id": fid, "card_id": cid}, None)
        assert "locked" in ran["error"].lower()
        attached = tools._tool_attach_file(
            user_obj, {"file_id": fid, "name": "x.txt", "content": "x"},
            None)
        assert "locked" in attached["error"].lower()
        listing = tools._tool_list(user_obj, {}, None)
        entry = next(n for n in listing["dashboards"] if n["id"] == fid)
        assert entry["ai_lock"] is True


def test_ai_hidden_blocks_everything(app, user, enabled):
    """Fix round (Important 1): a hidden dashboard is invisible to the AI
    tools — read/mutate/run/recommend refuse, list drops it."""
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Secret")
    _add_card(app, fid, title="KPI")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["ai_hidden"] = True
        dash_doc.save_document(stored, doc, force_checkpoint=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert "hidden" in tools._tool_read(
            user_obj, {"file_id": fid}, None)["error"]
        assert "hidden" in tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "kpi"}, None)["error"]
        assert "hidden" in tools._tool_run_card(
            user_obj, {"file_id": fid, "card_id": "c1"}, None)["error"]
        assert "hidden" in tools._tool_recommend(
            user_obj, {"file_id": fid}, None)["error"]
        listing = tools._tool_list(user_obj, {}, None)
        assert not any(n["id"] == fid for n in listing["dashboards"])


def test_tool_run_refuses_already_running_card(app, user, enabled):
    """Fix round (Important 2 pin, tools side): a second AI run on an
    in-flight card is refused — never a second concurrent execution."""
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid)
    _set_running(app, fid, cid)
    with app.app_context():
        user_obj = db.session.get(User, user)
        out = tools._tool_run_card(
            user_obj, {"file_id": fid, "card_id": cid}, None)
    assert cid in out["already_running"]


def test_console_same_card_run_guard():
    """Fix round (Important 2 pin, client side): both stream call sites
    refuse in-flight/running cards through one guard, so a second
    same-card stream — the only way to overwrite a live reader — cannot
    start (the server 409s it too, before any reader is created)."""
    import re
    import shutil
    import subprocess
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    match = re.search(r"function canStartCardRun\(V, card\) \{(.*?)\n    \}",
                      src, re.S)
    assert match, "canStartCardRun guard missing"
    # Both stream call sites gate on it; the rationale is documented.
    assert src.count("canStartCardRun(V, card") >= 2
    assert "Same-card concurrency is impossible" in src
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (match.group(0) + """
var V = { inFlight: {} };
var idle = { id: "c1", status: "idle" };
var running = { id: "c2", status: "running" };
var checks = [canStartCardRun(V, idle) === true,
    canStartCardRun(V, null) === false,
    canStartCardRun(V, running) === false];
V.inFlight["c1"] = true;
checks.push(canStartCardRun(V, idle) === false);
delete V.inFlight["c1"];
checks.push(canStartCardRun(V, idle) === true);
console.log(JSON.stringify(checks));
""")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == [True] * 5


# ---------------------------------------------------------------------------
# Dashboard script cards — Task A1: card schema (script_enabled + script)
# ---------------------------------------------------------------------------

def test_script_fields_default_off():
    card = dash_doc.new_card("kpi")
    assert card["script_enabled"] is False and card["script"] == ""


def test_workflow_card_rejects_script_toggle():
    card = dash_doc.new_card("workflow")
    card["script_enabled"] = True
    with pytest.raises(ValueError):
        dash_doc._validate_card(card)


def test_script_size_cap():
    card = dash_doc.new_card("table")
    card["script_enabled"] = True
    card["script"] = "x" * (dash_doc.SCRIPT_MAX_CHARS + 1)
    with pytest.raises(ValueError):
        dash_doc._validate_card(card)


# ---------------------------------------------------------------------------
# Dashboard script cards — Task A2: engine (_run_script_card + run_card)
# ---------------------------------------------------------------------------

def _script_card(app, fid, cid, script="res = 1\nres"):
    """A script-mode card dict (in-memory; run_card takes the card directly,
    so no save round-trip is needed — resolve against the live doc)."""
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        card = next(c for c in dash_doc.load(stored)["cards"]
                    if c["id"] == cid)
        card["script_enabled"] = True
        card["script"] = script
        return card


def test_script_card_fails_closed_without_pyrun(app, user, enabled,
                                                monkeypatch):
    """Task A2: pyrun disabled → run_card raises ValueError naming pyrun
    (the route settle path turns it into the card error state); the prompt
    path must not run instead (no AI steps for script cards)."""
    from app.services import module_service
    dash_engine = _engine()
    monkeypatch.setattr(module_service, "is_enabled",
                        lambda name: name != "pyrun")

    def boom_agent(user, prompt, drive=None, max_steps=None):
        raise AssertionError("prompt path must not run for script cards")

    def boom_complete(user, messages):
        raise AssertionError("prompt path must not run for script cards")

    monkeypatch.setattr(dash_engine, "run_agent_collected", boom_agent)
    monkeypatch.setattr(dash_engine, "complete_once", boom_complete)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid)
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="",
                    attachments=[att["id"]])
    card = _script_card(app, fid, cid)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        with pytest.raises(ValueError, match="pyrun"):
            dash_engine.run_card(stored, card, user)


def test_script_result_coercion_fallback(app, user, enabled, monkeypatch):
    """Task A2: a prose script value coerces through the card's contract —
    never an error state; (result, [], trace) contract holds, trace
    carrying the console entries."""
    from app.services import module_service
    dash_engine = _engine()
    monkeypatch.setattr(module_service, "is_enabled", lambda name: True)
    seen = {}

    def fake_evaluate(code, files):
        seen["files"] = files
        return {"ok": True, "value": "some prose"}

    monkeypatch.setattr(dash_engine, "_pyrun_evaluate", fake_evaluate,
                        raising=False)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="")
    card = _script_card(app, fid, cid,
                          script="res = 'some prose'\nres")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        result, artifacts, trace = dash_engine.run_card(stored, card, user)
    assert result.get("text") == "some prose"  # never an error state
    assert artifacts == []
    # Script runs carry a console trace (attachments + timing); the empty
    # trace was the pre-console behavior, not the contract.
    assert isinstance(trace, list) and len(trace) >= 1
    assert seen["files"] == {}  # no attachments → empty files map


def test_script_run_trace_lists_inputs_and_timing(app, user, enabled,
                                                 monkeypatch):
    """Script runs record a console trace: resolved inputs (name → table)
    plus elapsed milliseconds — the View run console shows this."""
    from app.services import module_service
    dash_engine = _engine()
    monkeypatch.setattr(module_service, "is_enabled", lambda name: True)

    def fake_evaluate(code, files):
        return {"ok": True, "value": {"value": "3", "label": "rows"}}

    monkeypatch.setattr(dash_engine, "_pyrun_evaluate", fake_evaluate,
                        raising=False)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    first = _attach_file(app, fid, name="a.csv", content=b"v\n1\n")
    second = _attach_file(app, fid, name="b.csv", content=b"v\n2\n")
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="",
                    attachments=[first["id"], second["id"]])
    card = _script_card(app, fid, cid, script="res = 1\nres")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        _, _, trace = dash_engine.run_card(stored, card, user)
    by_label = {e.get("label"): e.get("detail", "") for e in trace}
    assert "a.csv" in by_label.get("script", "")
    assert "b.csv" in by_label.get("script", "")
    assert "t0" in by_label.get("script", "")
    assert "t1" in by_label.get("script", "")
    assert "ms" in by_label.get("timing", "")


def test_kpi_view_sign_classes():
    """KPI view polish: delta sign classes branch in viewKpiHTML and the
    CSS carries the big-number treatment."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "kpiDeltaClass" in src
    assert "dash-kpi-d-pos" in src and "dash-kpi-d-neg" in src
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    assert "clamp(36px, 4vw, 56px)" in css
    assert "dash-kpi-d-pos" in css and "dash-kpi-d-neg" in css
    assert ".dash-pager" in css


def test_builder_popup_editor_hooks():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "openEditor" in src
    assert "dash-codeedit" in src and "dash-codeedit-in" in src
    assert "hljs" in src and "python" in src
    assert 'data-zone="edit-save"' in src and 'data-zone="edit-close"' in src
    assert 'data-dlg="edit"' in src
    assert "highlight unavailable" in src and "highlight off" in src


def test_builder_card_script_overlay():
    """Card script fields render the same highlight overlay as the popup
    (shared painter), compact variant for the 3-row preview."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "dash-codeedit-card" in src
    assert "paintScriptHighlight" in src
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    assert ".dash-codeedit-card" in css
    # Close lifecycle: dirty → confirm, no double-prompt, guard survives.
    assert "Discard changes?" in src
    assert "S._edit.saved = true" in src
    assert "ctrlKey" in src and "metaKey" in src
    assert "scheduleSave(S)" in src
    # Highlight fallback: plain textarea stands if hljs fails.
    assert "window.hljs.highlight" in src
    assert "never silent" in src
    # Inline palette: spans carry their own color so no stylesheet,
    # override or contrast theme can wash them out.
    assert "HL_COLORS" in src
    assert "forced-color-adjust" in css
    assert "ensureOverlay" in src
    # Popup inputs honor the same caps as the card fields.
    assert "maxlength=\"' + cap + '\"" in src


def test_builder_card_content_first():
    """Content-first builder card: labeled Edit pills open the popup;
    script/tools/workflow controls live in a Settings drawer (workflow
    cards keep the linked picker visible); expand hooks are kept."""
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "dash-edit-btn" in src and ">Edit</button>" in src
    assert "dash-settings" in src
    assert 'data-f="expand-prompt"' in src
    assert 'data-f="expand-script"' in src
    assert "if (scriptToggle)" in src  # guard: workflow cards omit the checkbox
    assert '<div class="dash-wf"><span class="dash-prompt-label">Linked workflow</span>' in src
    assert "on = full agent · off = 1 LLM call" in src
    assert "truncation flagged" not in src  # long hint trimmed to one line
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        css = fh.read()
    assert ".dash-edit-btn" in css
    assert "data-tpage-btn" in src and "dash-pager-info" in src


def test_script_table_json_string_coerces():
    """Script cards often return JSON text (not dicts): a JSON string that
    decodes to a table shape must coerce into the table snapshot instead
    of rendering as raw JSON text — including the {"table": ...} wrapper
    some scripts return."""
    dash_engine = _engine()
    s = json.dumps({"headers": ["Tabela", "Registos"],
                    "rows": [["cmdb_ci_network_adapter", 729]]})
    out = dash_engine._parse_script_result("table", s)
    assert out["table"]["headers"] == ["Tabela", "Registos"]
    assert out["table"]["rows"] == [["cmdb_ci_network_adapter", "729"]]
    assert out["total_rows"] == 1 and out["truncated_rows"] is False
    out = dash_engine._parse_script_result(
        "table", json.dumps({"table": {"headers": ["A"],
                                       "rows": [["1"]]}}))
    assert out["table"] == {"headers": ["A"], "rows": [["1"]]}
    # Non-JSON prose and non-table JSON keep the text fallback.
    assert dash_engine._parse_script_result(
        "table", "just prose") == {"text": "just prose"}
    out = dash_engine._parse_script_result("table", '{"a": 1}')
    assert out == {"text": '{"a": 1}'}


def test_script_card_reads_whole_file(app, user, monkeypatch):
    """Script cards must see whole attachments: a 250KB CSV (over the old
    200K-char read cap) counts every row, not just the prefix."""
    from app.services import module_service
    dash_engine = _engine()
    monkeypatch.setattr(module_service, "is_enabled", lambda name: True)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    n = 20000
    content = "id,val\n" + "".join(f"{i},x{i:06d}\n" for i in range(n))
    assert len(content) > 200_000
    att = _attach_file(app, fid, name="big.csv",
                       content=content.encode("utf-8"))
    cid = _add_card(app, fid, type="kpi", title="Count", prompt="",
                    attachments=[att["id"]])
    card = _script_card(app, fid, cid,
                        script="res = db.query('select count(*) as n from t0')[0]['n']\n"
                               "{'value': str(res)}")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        result, _, _ = dash_engine.run_card(stored, card, user)
    assert result == {"kpi": {"value": str(n)}}, result


def test_script_value_dict_coerces_to_kpi():
    """Script cards: a returned {"value", "label"?, "delta"?} dict takes
    the KPI big-number path (viewKpiHTML), not the text fallback — this
    is how script KPIs get the big-number rendering."""
    dash_engine = _engine()
    assert dash_engine._parse_script_result(
        "kpi", {"value": "3.509,5 kWh",
                "label": "3.509.549,78 Wh"}) == {
        "kpi": {"value": "3.509,5 kWh", "label": "3.509.549,78 Wh"}}
    assert dash_engine._parse_script_result(
        "kpi", {"value": "410,0 kWh", "label": "mar/2026"}) == {
        "kpi": {"value": "410,0 kWh", "label": "mar/2026"}}


def test_script_table_dict_coerces_to_snapshot():
    """Script cards: {"headers", "rows"} dicts coerce into the table
    snapshot (same caps/guards as the pipe path); ragged rows pad/cut."""
    dash_engine = _engine()
    assert dash_engine._parse_script_result(
        "table", {"headers": ["Mês", "kWh"],
                  "rows": [["jan/2026", "410,0"],
                           ["fev/2026"],
                           ["mar/2026", "377,8", "EXTRA"]]}) == {
        "table": {"headers": ["Mês", "kWh"],
                  "rows": [["jan/2026", "410,0"],
                           ["fev/2026", ""],
                           ["mar/2026", "377,8"]]},
        "total_rows": 3, "truncated_rows": False}


def test_script_row_list_coerces_to_snapshot():
    """Script cards: a bare list of row dicts (db.query passthrough)
    coerces with headers from the key union in first-seen order."""
    dash_engine = _engine()
    assert dash_engine._parse_script_result(
        "table", [{"Mês": "jan/2026", "kWh": "410,0"},
                  {"Mês": "fev/2026"}]) == {
        "table": {"headers": ["Mês", "kWh"],
                  "rows": [["jan/2026", "410,0"],
                           ["fev/2026", ""]]},
        "total_rows": 2, "truncated_rows": False}


def test_script_table_caps_rows_and_flags():
    """Script tables cap at 500 rows with total + truncated flag."""
    dash_engine = _engine()
    rows = [[str(i)] for i in range(600)]
    out = dash_engine._parse_script_result(
        "table", {"headers": ["n"], "rows": rows})
    assert out["total_rows"] == 600
    assert out["truncated_rows"] is True
    assert len(out["table"]["rows"]) == 500


def test_script_files_map_is_positional(app, user, enabled, monkeypatch):
    """Task A2 (amended, script-card bugfix): the pyrun files map is
    positional in card attachment order — files_map["f{pos}"] only, each a
    [read_text_content] chunk list. No "f" alias: pyrun seeds tables t0..tN
    in files-dict order, so an extra "f" key would duplicate the first
    file at t1 and shift every later table."""
    from app.services import module_service
    dash_engine = _engine()
    monkeypatch.setattr(module_service, "is_enabled", lambda name: True)
    seen = {}

    def fake_evaluate(code, files):
        seen["call"] = (code, files)
        return {"ok": True, "value": {"value": "3", "label": "rows"}}

    monkeypatch.setattr(dash_engine, "_pyrun_evaluate", fake_evaluate,
                        raising=False)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    first = _attach_file(app, fid, name="a.csv", content=b"v\n1\n")
    second = _attach_file(app, fid, name="b.csv", content=b"v\n2\n")
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="",
                    attachments=[first["id"], second["id"]])
    card = _script_card(app, fid, cid,
                          script="res = len(rows('f0'))\nres")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        result, _, _ = dash_engine.run_card(stored, card, user)
    code, files = seen["call"]
    assert code == "res = len(rows('f0'))\nres"
    assert set(files) == {"f0", "f1"}
    assert files["f0"] != files["f1"]
    assert all(isinstance(chunks, list) and len(chunks) == 1
               for chunks in files.values())
    assert "v" in files["f0"][0] and "v" in files["f1"][0]
    assert result == {"kpi": {"value": "3", "label": "rows"}}


def test_script_tables_follow_attachment_order(app, user, enabled,
                                                 monkeypatch):
    """Script-card bugfix: with real pyrun evaluation, tables t0..tN hold
    the attachments' contents in card order — no duplication, no shift."""
    from app.services import module_service
    dash_engine = _engine()
    monkeypatch.setattr(module_service, "is_enabled", lambda name: True)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    first = _attach_file(app, fid, name="a.csv", content=b"id\nA1\nA2\n")
    second = _attach_file(app, fid, name="b.csv", content=b"id\nB1\nB2\n")
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="",
                    attachments=[first["id"], second["id"]])
    card = _script_card(
        app, fid, cid,
        script="a = db.query('select id from t0')[0]['id']\n"
               "b = db.query('select id from t1')[0]['id']\n"
               "{'t0id': a, 't1id': b}")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        result, _, _ = dash_engine.run_card(stored, card, user)
    # A bare dict is not the KPI {"value", "label"} contract, so the value
    # falls back to {"text": ...} — but the table data inside proves t0/t1
    # hold the right files in order (no duplication, no shift).
    assert result == {'text': '{"t0id": "A1", "t1id": "B1"}'}, result


def test_script_card_rejects_foreign_attachment(app, user, enabled,
                                                monkeypatch):
    """Task A2: an attachment entry pointing at another user's file →
    MissingInputError (ownership is re-checked at run time, never silent)."""
    from app.services import module_service
    dash_engine = _engine()
    monkeypatch.setattr(module_service, "is_enabled", lambda name: True)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    att = _attach_file(app, fid)
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="",
                    attachments=[att["id"]])
    with app.app_context():
        bob = User(username="bob", email="bob@example.com")
        bob.set_password("password123")
        db.session.add(bob)
        db.session.commit()
        asset = db.session.get(StoredFile, att["fileId"])
        asset.user_id = bob.id  # entry now points at another user's file
        db.session.commit()
        stored = db.session.get(StoredFile, fid)
        card = next(c for c in dash_doc.load(stored)["cards"]
                    if c["id"] == cid)
        card["script_enabled"] = True
        card["script"] = "res = 1\nres"
        with pytest.raises(dash_engine.MissingInputError):
            dash_engine.run_card(stored, card, user)


# ---------------------------------------------------------------------------
# Task A3: AI tools accept script card fields
# ---------------------------------------------------------------------------

def test_add_card_accepts_script_fields(app, user, enabled):
    tools = _dash_tools()
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        fid = tools._tool_create(
            user_obj, {"name": "Dash"}, None)["created"]["id"]
        outcome = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "kpi",
                        "script_enabled": True, "script": "res = 1\nres"},
            None)
        assert outcome["card"]["script_enabled"] is True
        assert outcome["card"]["script"] == "res = 1\nres"


def test_update_card_sets_script(app, user, enabled):
    tools = _dash_tools()
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        fid = tools._tool_create(
            user_obj, {"name": "Dash"}, None)["created"]["id"]
        added = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "kpi",
                        "title": "Card", "prompt": "Say hi"}, None)
        cid = added["card"]["id"]
        nested = tools._tool_update_card(
            user_obj, {"file_id": fid, "card_id": cid,
                        "card": {"script_enabled": True,
                                 "script": "res = 1\nres"}}, None)
        assert nested["card"]["script_enabled"] is True
        assert nested["card"]["script"] == "res = 1\nres"
        flat = tools._tool_update_card(
            user_obj, {"file_id": fid, "card_id": cid,
                        "script": "res = 2\nres",
                        "card": {"script": "res = 9\nres"}}, None)
        assert flat["card"]["script"] == "res = 2\nres"


# ---------------------------------------------------------------------------
# Task A4: builder toggle + editor, immediate run, stream branch
# ---------------------------------------------------------------------------

def test_script_builder_hooks():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert 'data-f="script_enabled"' in src and 'data-f="script"' in src
    assert "dash-script" in src
    assert 'data-f="promptwrap"' in src and 'data-f="scriptwrap"' in src
    i = src.find("dash-script-toggle")
    assert i >= 0 and "workflow" in src[max(0, i - 200):i + 200]


def _script_card_persisted(app, fid, cid, script="res = 1\nres"):
    """Flip a saved card to script mode (the stream/classic routes read the
    stored doc, so the toggle must be persisted, not just in-memory)."""
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        card = next(c for c in doc["cards"] if c["id"] == cid)
        card["script_enabled"] = True
        card["script"] = script
        dash_doc.save_document(stored, doc)


def _stub_no_agent(monkeypatch, evaluate=None):
    """pyrun on, every AI seam rigged to blow: any agent use fails the test."""
    from app.services import module_service
    dash_engine = _engine()
    outcome = {"ok": True, "value": "seven"} if evaluate is None else evaluate
    monkeypatch.setattr(module_service, "is_enabled", lambda name: True)
    monkeypatch.setattr(dash_engine, "_pyrun_evaluate",
                        lambda code, files: outcome,
                        raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("agent must not run for script cards")

    monkeypatch.setattr(dash_engine, "run_agent_collected", boom)
    monkeypatch.setattr(dash_engine, "complete_once", boom)
    monkeypatch.setattr("app.services.agent_service.run_agent_events", boom)


def test_script_run_needs_no_prompt(app, auth_client, user, enabled,
                                    monkeypatch):
    """Task A4 run-immediately contract: the existing Refresh POST runs a
    script card with an empty prompt — no prompt required, no AI steps."""
    _stub_no_agent(monkeypatch)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="")
    _script_card_persisted(app, fid, cid)
    assert _run_and_finish(app, auth_client, fid, cid) == {"text": "seven"}


def test_script_stream_runs_script(app, auth_client, user, enabled,
                                   monkeypatch):
    """Task A4: a script-enabled card via /run/stream executes the script
    through run_card — the done frame carries the coerced script result and
    the card settles done with zero agent calls."""
    _stub_no_agent(monkeypatch)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="")
    _script_card_persisted(app, fid, cid)
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    body = b"".join(resp.response).decode()
    assert '"t": "done"' in body
    assert "seven" in body
    card = _card(app, fid, cid)
    assert card["status"] == "done"
    assert card["result"] == {"text": "seven"}


def test_script_stream_error_settles_error(app, auth_client, user, enabled,
                                           monkeypatch):
    """Fix wave: a failing script via /run/stream emits the normal done
    frame carrying the error and settles the card error — zero agent calls."""
    _stub_no_agent(monkeypatch,
                   evaluate={"ok": False, "error": "boom"})
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="")
    _script_card_persisted(app, fid, cid)
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    body = b"".join(resp.response).decode()
    assert '"t": "done"' in body
    assert "boom" in body
    card = _card(app, fid, cid)
    assert card["status"] == "error"
    assert card["result"] == {"error": "boom"}


def test_update_card_normalizes_script(app, user, enabled):
    """Fix wave: the update path normalizes script like the add path
    (str(...), None → "")."""
    tools = _dash_tools()
    _ = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        fid = tools._tool_create(
            user_obj, {"name": "Dash"}, None)["created"]["id"]
        added = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "kpi",
                        "title": "Card", "prompt": "Say hi"}, None)
        cid = added["card"]["id"]
        nulled = tools._tool_update_card(
            user_obj, {"file_id": fid, "card_id": cid,
                        "card": {"script": None}}, None)
        assert nulled["card"]["script"] == ""
        coerced = tools._tool_update_card(
            user_obj, {"file_id": fid, "card_id": cid,
                        "script": 123}, None)
        assert coerced["card"]["script"] == "123"


def test_parse_script_result_maps_none_to_empty():
    """Fix wave: a None script value coerces to "" — never a literal
    "None" snapshot."""
    assert _engine()._parse_script_result("kpi", None) == {"text": ""}


# ---------------------------------------------------------------------------
# Task C1: row equalization + snap guides + size presets
# ---------------------------------------------------------------------------

def test_row_equalization_caps_markdown():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "c.h === first.h" in src  # same-row rule, type check dropped
    assert "c.type === first.type" not in src  # no same-type gate
    assert "snap-guide" in src or "grid-guide" in src
    assert "dash-size-menu" in src  # badge opens a quick-select dropdown
    assert "3×1" in src and "12×4" in src  # quick-select presets


# ---------------------------------------------------------------------------
# Task C2: guided workflow picker + live run progress
# ---------------------------------------------------------------------------

def _dash_js_src():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.js"),
              encoding="utf-8") as fh:
        return fh.read()


def _workflow_config_region(src):
    """The linked-workflow field block (wfFields): from its grid wrapper
    to the Settings drawer that follows it."""
    start = src.index("dash-wf-grid")
    end = src.index("dash-settings", start)
    return src[start:end]


def test_workflow_picker_uses_dropdown_and_contract():
    src = _dash_js_src()
    assert "AutomationRunForm" in src and "run-contract" in src
    assert "dash-wf-select" in src  # <select> of the user's workflows
    region = _workflow_config_region(src)
    assert 'data-f="wfparams"' not in region  # manual JSON textarea gone
    assert 'inputmode="numeric"' not in region  # raw numeric id gone


def test_workflows_endpoint_lists_owned(app, auth_client, user, enabled):
    wid = _workflow_id(app, user)
    with app.app_context():
        db.session.add(ModuleState(name="automation", enabled=True))
        db.session.commit()
    body = auth_client.get("/m/dashboards/workflows").get_json()
    assert body["ok"] and body["automation_enabled"]
    assert {"id", "name"} <= set(body["workflows"][0])
    assert wid in {w["id"] for w in body["workflows"]}


def test_workflows_endpoint_fails_closed_when_automation_off(
        app, auth_client, user, enabled):
    body = auth_client.get("/m/dashboards/workflows").get_json()
    assert not body["ok"] and not body["automation_enabled"]
    assert "automation" in body["error"].lower()


def test_workflow_stream_deleted_midrun(app, auth_client, user, enabled,
                                        monkeypatch):
    """C2: a workflow deleted mid-run settles the card error and the SSE
    stream still closes cleanly (done frame, no hang, no 500)."""
    dash_engine = _engine()
    wid = _workflow_id(app, user)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="workflow", prompt="Summarize",
                    workflow={"workflow_id": wid, "params": {}})

    real_start = dash_engine.start_workflow_run

    def start_then_delete(workflow, run_user, params, **kw):
        run = real_start(workflow, run_user, params, **kw)
        with app.app_context():
            doomed = db.session.get(
                dash_engine._automation_models().AutomationWorkflow, wid)
            db.session.delete(doomed)
            db.session.commit()
        return run

    monkeypatch.setattr(dash_engine, "start_workflow_run", start_then_delete)
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    body = b"".join(resp.response).decode()
    assert '"t": "done"' in body
    card = _card(app, fid, cid)
    assert card["status"] == "error"
    assert card["result"]["error"]


def test_workflow_stream_emits_node_progress(app, auth_client, user,
                                             enabled, fake_ai):
    """C2: a workflow stream surfaces intermediate node progress frames
    (live console) before the done frame — not a silent wait."""
    wid = _workflow_id(app, user)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, type="workflow", prompt="Summarize",
                    workflow={"workflow_id": wid, "params": {}})
    resp = auth_client.post(f"/m/dashboards/{fid}/run/stream",
                            json={"card_id": cid}, buffered=False)
    assert resp.status_code == 200
    body = b"".join(resp.response).decode()
    assert '"t": "step"' in body
    assert '"t": "done"' in body
    assert _card(app, fid, cid)["status"] == "done"


# ---------------------------------------------------------------------------
# Task C3: interactive table widgets + chart kinds + editable title
# ---------------------------------------------------------------------------

def test_chart_result_accepts_new_kinds():
    dash_engine = _engine()
    res = dash_engine._parse_chart_result(
        '{"chart": {"kind": "doughnut", "labels": ["a"], '
        '"datasets": [{"label": "x", "data": [1]}]}}')
    assert res["chart"]["kind"] == "doughnut"


def test_chart_script_result_accepts_new_kinds():
    """C3 (controller pin): script cards coerce through the same chart
    contract, so one engine change covers both paths."""
    dash_engine = _engine()
    for kind in ("doughnut", "horizontalBar", "area"):
        value = {"chart": {"kind": kind, "labels": ["a"],
                           "datasets": [{"label": "x", "data": [1]}]}}
        res = dash_engine._parse_script_result("chart", value)
        assert res["chart"]["kind"] == kind


def test_chart_contract_lists_new_kinds():
    dash_engine = _engine()
    contract = dash_engine._OUTPUT_CONTRACTS["chart"]
    for kind in ("doughnut", "horizontalBar", "area"):
        assert kind in contract


def test_view_widgets_present():
    src = _dash_js_src()
    assert "dash-sort" in src and "Download CSV" in src
    assert "dash-chart-switch" in src or "chart-switch" in src


def test_view_table_pager_composition():
    """C3 (controller pin): sort covers the full snapshot rows (not the
    visible page); the filter resets to page 1."""
    src = _dash_js_src()
    assert "tableViewRows" in src  # filtered+sorted FULL rows, paged after
    assert "sortCol" in src and "sortDir" in src
    assert "dash-filter" in src


def test_csv_download_matches_view():
    """C3 (review focus 3): Download CSV exports the filtered/sorted view
    rows — the full snapshot, not the visible page."""
    src = _dash_js_src()
    assert "Download CSV" in src
    assert "tableToCSV" in src
    assert "data-dash-csv" in src


def test_dashboard_rename_syncs_title_and_name(app, auth_client, user,
                                               enabled):
    """C3 (notebooks N1 mirror): POST /m/dashboards/<id>/rename syncs
    stored.name and doc["title"] together through the same route shape."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Original")
    auth_client.post(f"/m/dashboards/{fid}/rename", data={"name": "novo"})
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.name == "novo.pdash"
        assert dash_doc.load(stored)["title"] == "novo"
    auth_client.post(f"/m/dashboards/{fid}/rename",
                     data={"name": "  segundo  "})
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.name == "segundo.pdash"
        assert dash_doc.load(stored)["title"] == "segundo"


def test_dashboard_title_input_wired():
    """C3: edit.html exposes an editable title input + renameUrl (no
    static h1-only header), bound in the builder JS."""
    with open(os.path.join(_DASH_DIR, "templates", "dashboards",
                           "edit.html"), encoding="utf-8") as fh:
        html = fh.read()
    src = _dash_js_src()
    assert "dash-title" in html and "renameUrl" in html
    assert "dash-title" in src and "renameUrl" in src


# ---------------------------------------------------------------------------
# Task C4: dashboard variables + View filter bar
# ---------------------------------------------------------------------------

def test_variables_substitute_and_rerun_affected_only(
        app, auth_client, user, enabled, fake_ai, monkeypatch):
    """C4 (brief + review focus 4): {{ region }} substitutes into the prompt
    AND the script source before each run; after a value change only cards
    referencing the variable re-run — the rest keep status done + ranAt."""
    from app.services import module_service
    dash_engine = _engine()
    monkeypatch.setattr(module_service, "is_enabled", lambda name: True)
    seen = {}

    def fake_evaluate(code, files):
        seen["code"] = code
        return {"ok": True, "value": "script says " + code}

    monkeypatch.setattr(dash_engine, "_pyrun_evaluate", fake_evaluate,
                        raising=False)
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    var_cid = _add_card(app, fid, type="kpi", title="Regional",
                        prompt="Count incidents in {{ region }}")
    plain_cid = _add_card(app, fid, type="kpi", title="Static",
                          prompt="Count everything")
    script_cid = _add_card(app, fid, type="kpi", title="Scripted", prompt="")
    _script_card_persisted(app, fid, script_cid,
                           script="res = '{{ region }}'\nres")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["variables"] = {"region": "north"}
        dash_doc.save_document(stored, doc)

    assert _run_and_finish(app, auth_client, fid,
                           var_cid) == {"text": "3 open incidents"}
    assert "north" in fake_ai["agent_prompt"]
    assert "{{ region }}" not in fake_ai["agent_prompt"]
    assert _run_and_finish(app, auth_client, fid, script_cid)
    assert "north" in seen["code"]
    assert "{{" not in seen["code"]

    resp = auth_client.post(f"/m/dashboards/{fid}/run", json={"all": True})
    assert resp.status_code == 200
    for cid in (var_cid, plain_cid, script_cid):
        assert _card(app, fid, cid)["status"] == "done"
    plain_ran_at = _card(app, fid, plain_cid)["ranAt"]

    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["variables"] = {"region": "south"}
        dash_doc.save_document(stored, doc)
        affected = dash_engine.affected_cards(doc, ["region"])
    assert set(affected) == {var_cid, script_cid}

    # The View filter bar runs only the affected cards (never a blind all).
    for cid in affected:
        _run_and_finish(app, auth_client, fid, cid)
    assert "south" in fake_ai["agent_prompt"]
    assert "south" in seen["code"]
    untouched = _card(app, fid, plain_cid)
    assert untouched["status"] == "done"
    assert untouched["ranAt"] == plain_ran_at


def test_variables_reject_bad_shape():
    doc = dash_doc.empty_dashboard("T")
    with pytest.raises(ValueError):
        dash_doc.validate({**doc, "variables": ["not", "an", "object"]})


def test_variables_validate_scalars_and_names():
    """C4: variables default to {}, accept scalar defaults, reject bad
    names and non-scalar values (C5 pre-fill contract)."""
    doc = dash_doc.empty_dashboard("T")
    assert dash_doc.validate(dict(doc))["variables"] == {}
    good = dash_doc.validate({**doc, "variables": {
        "region": "north", "limit": 10, "ratio": 1.5,
        "flag": True, "empty": None}})
    assert good["variables"] == {"region": "north", "limit": 10,
                                 "ratio": 1.5, "flag": True, "empty": None}
    for bad in ({"a b": 1}, {"": 1}, {"9lives": 1}, {"region": {"n": 1}},
                {"region": ["x"]}, {"region": "x" * 2001}):
        with pytest.raises(ValueError):
            dash_doc.validate({**doc, "variables": bad})


def test_variables_substitute_helper():
    """C4 (delta): one substitution helper serves prompts AND script
    source, so script cards stay consistent; unknown names stay literal."""
    dash_engine = _engine()
    sub = dash_engine.substitute_variables
    assert sub("Hi {{ region }}!", {"region": "north"}) == "Hi north!"
    assert sub("{{a}}{{ a }}", {"a": 1}) == "11"
    assert sub("Keep {{ unknown }}.", {}) == "Keep {{ unknown }}."
    assert sub("v={{v}}", {"v": None}) == "v="
    assert sub("res = '{{ region }}'", {"region": "s"}) == "res = 's'"
    assert sub("", {"a": 1}) == ""
    uses = dash_engine.card_uses_variable
    assert uses({"prompt": "x {{ region }}", "script": ""}, "region")
    assert uses({"prompt": "", "script": "v='{{region}}'"}, "region")
    assert not uses({"prompt": "no refs", "script": ""}, "region")
    assert not uses({"prompt": "{{ regional }}", "script": ""}, "region")


def test_view_filter_bar_present():
    """C4: View mode renders a variables filter bar that re-runs only
    cards referencing a changed variable (affected-only, rest untouched)."""
    src = _dash_js_src()
    assert "dash-varbar" in src  # bar mount in the View skeleton
    assert "cardUsesVariable" in src  # prompt/script reference scan
    assert "runAffected" in src  # affected-only re-run entry point


def test_script_tool_documents_variables():
    """C4 (delta): the script tool description documents {{ var }}
    substitution (db.tables() needs none — it takes no prompt text)."""
    with open(os.path.join(_DASH_DIR, "tools.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    i = src.find('"script": {"type": "string"')
    assert i >= 0
    region = src[i:i + 1200]
    assert "{{" in region and "variable" in region.lower()


# ---------------------------------------------------------------------------
# Task C5: "Generate with AI" creation wizard
# ---------------------------------------------------------------------------

def _list_html():
    with open(os.path.join(_DASH_DIR, "templates", "dashboards",
                           "list.html"), encoding="utf-8") as fh:
        return fh.read()


def _wizard_fid(app, auth_client, drive_id, data):
    before = _pdash_count(app)
    resp = auth_client.post("/m/dashboards/new", data=data,
                            content_type="multipart/form-data")
    assert resp.status_code == 302, resp.data[:300]
    assert _pdash_count(app) == before + 1
    return int(resp.headers["Location"].rsplit("/", 1)[-1])


def _pdash_count(app):
    from app.models import StoredFile as _SF
    with app.app_context():
        return _SF.query.filter_by(extension="pdash").count()


def _wizard_doc(app, fid):
    from app.models import StoredFile as _SF
    with app.app_context():
        return dash_doc.load(db.session.get(_SF, fid))


def test_generate_wizard_hooks():
    html = _list_html()
    assert "dash-wizard" in html or "generate" in html.lower()


def test_wizard_create_generates_layout(app, auth_client, user, enabled):
    """C5: a description in the wizard step auto-generates an initial
    multi-card layout on creation (prompts carry the description)."""
    drive_id = _drive(app, user)
    fid = _wizard_fid(app, auth_client, drive_id,
                      {"name": "Tracked", "location": f"drive:{drive_id}",
                       "description": "Weekly server incidents"})
    doc = _wizard_doc(app, fid)
    assert {c["type"] for c in doc["cards"]} == {
        "kpi", "table", "chart", "markdown"}
    assert all("Weekly server incidents" in (c["prompt"] or "")
               for c in doc["cards"])


def test_wizard_create_prefills_variables(app, auth_client, user, enabled):
    """C5: wizard output pre-fills C4 variables from plain form values, and
    generated prompts reference them (affected-only re-run stays
    meaningful)."""
    drive_id = _drive(app, user)
    fid = _wizard_fid(
        app, auth_client, drive_id,
        {"name": "Regional", "location": f"drive:{drive_id}",
         "description": "Incidents by data center",
         "variables": '{"region": "north", "limit": 5}'})
    doc = _wizard_doc(app, fid)
    assert doc["variables"] == {"region": "north", "limit": 5}
    assert any("{{ region }}" in (c["prompt"] or "")
               for c in doc["cards"])


def test_wizard_create_rejects_bad_variables(app, auth_client, user,
                                             enabled):
    """C5: bad variables (malformed JSON or non-scalar shape) abort 400
    through the C4 contract — never leaving an empty dashboard behind."""
    drive_id = _drive(app, user)
    for bad_vars in ('{"region":', '{"region": {"n": 1}}', '["x"]'):
        before = _pdash_count(app)
        resp = auth_client.post(
            "/m/dashboards/new",
            data={"name": "Bad", "location": f"drive:{drive_id}",
                  "description": "Something", "variables": bad_vars},
            content_type="multipart/form-data")
        assert resp.status_code == 400
        assert _pdash_count(app) == before


def test_wizard_create_attaches_uploads(app, auth_client, user, enabled):
    """C5: files attached in the wizard step land on the new dashboard via
    the existing attach path (upload origin)."""
    drive_id = _drive(app, user)
    fid = _wizard_fid(
        app, auth_client, drive_id,
        {"name": "With files", "location": f"drive:{drive_id}",
         "description": "Incidents in the CSV",
         "wizard_files": (io.BytesIO(b"a,b\n1,2\n"), "incidents.csv")})
    doc = _wizard_doc(app, fid)
    assert len(doc["attachments"]) == 1
    assert doc["attachments"][0]["origin"] == "upload"
    assert doc["attachments"][0]["name"] == "incidents.csv"


def test_wizard_plain_create_unchanged(app, auth_client, user, enabled):
    """C5: without wizard fields the plain name+location flow creates an
    empty dashboard exactly as before."""
    drive_id = _drive(app, user)
    fid = _wizard_fid(app, auth_client, drive_id,
                      {"name": "Plain", "location": f"drive:{drive_id}"})
    doc = _wizard_doc(app, fid)
    assert doc["cards"] == [] and doc["variables"] == {}


def test_wizard_js_wiring():
    """C5: the wizard step lives in dashboard.js (guarded init) and the
    list modal loads that bundle."""
    src = _dash_js_src()
    assert "initDashWizard" in src
    assert "dash-wizard" in src
    html = _list_html()
    assert "dashboard.js" in html


# ---------------------------------------------------------------------------
# Task C6: AI guards + live Builder poll
# ---------------------------------------------------------------------------

def _edit_html():
    with open(os.path.join(_DASH_DIR, "templates", "dashboards",
                           "edit.html"), encoding="utf-8") as fh:
        return fh.read()


def _dash_css():
    with open(os.path.join(_DASH_DIR, "static", "dashboard.css"),
              encoding="utf-8") as fh:
        return fh.read()


def test_ai_hidden_blocks_all_tools(app, user, enabled):
    """C6: a hidden dashboard refuses every file_id AI tool path, and the
    list tool drops it. (Creating a dashboard takes no file_id, so it
    stays usable — hiding applies per dashboard, not to creation.)"""
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Secret")
    cid = _add_card(app, fid, title="KPI")
    att = _attach_file(app, fid)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["ai_hidden"] = True
        dash_doc.save_document(stored, doc, force_checkpoint=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert "hidden" in tools._tool_read(
            user_obj, {"file_id": fid}, None)["error"]
        assert "hidden" in tools._tool_update(
            user_obj, {"file_id": fid, "title": "x"}, None)["error"]
        assert "hidden" in tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "kpi"}, None)["error"]
        assert "hidden" in tools._tool_update_card(
            user_obj, {"file_id": fid, "card_id": cid,
                       "title": "x"}, None)["error"]
        assert "hidden" in tools._tool_delete_card(
            user_obj, {"file_id": fid, "card_id": cid}, None)["error"]
        assert "hidden" in tools._tool_attach_file(
            user_obj, {"file_id": fid, "name": "y.txt",
                       "content": "y"}, None)["error"]
        assert "hidden" in tools._tool_detach_file(
            user_obj, {"file_id": fid,
                       "attachment_id": att["id"]}, None)["error"]
        assert "hidden" in tools._tool_run_card(
            user_obj, {"file_id": fid, "card_id": cid}, None)["error"]
        assert "hidden" in tools._tool_run_all(
            user_obj, {"file_id": fid}, None)["error"]
        assert "hidden" in tools._tool_recommend(
            user_obj, {"file_id": fid}, None)["error"]
        listing = tools._tool_list(user_obj, {}, None)
        assert not any(n["id"] == fid for n in listing["dashboards"])


def test_ai_lock_blocks_all_edits_but_allows_reads(app, user, enabled):
    """C6: a locked dashboard refuses every AI mutation path with the
    unlock message, while read/list/recommend stay usable."""
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    cid = _add_card(app, fid, title="KPI")
    att = _attach_file(app, fid)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        doc = dash_doc.load(stored)
        doc["ai_lock"] = True
        dash_doc.save_document(stored, doc, force_checkpoint=True)
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert "error" not in tools._tool_read(
            user_obj, {"file_id": fid}, None)
        assert "error" not in tools._tool_recommend(
            user_obj, {"file_id": fid}, None)
        for out in (
                tools._tool_update(
                    user_obj, {"file_id": fid, "title": "x"}, None),
                tools._tool_add_card(
                    user_obj, {"file_id": fid, "type": "kpi"}, None),
                tools._tool_update_card(
                    user_obj, {"file_id": fid, "card_id": cid,
                               "title": "x"}, None),
                tools._tool_delete_card(
                    user_obj, {"file_id": fid, "card_id": cid}, None),
                tools._tool_attach_file(
                    user_obj, {"file_id": fid, "name": "y.txt",
                               "content": "y"}, None),
                tools._tool_detach_file(
                    user_obj, {"file_id": fid,
                               "attachment_id": att["id"]}, None),
                tools._tool_run_card(
                    user_obj, {"file_id": fid, "card_id": cid}, None),
                tools._tool_run_all(
                    user_obj, {"file_id": fid}, None)):
            assert "locked" in out["error"].lower()


def test_ai_tool_mutations_emit_dashboard_changed(app, user, enabled):
    """C6 push leg: structural AI edits carry a dashboard_changed ui event
    (the agent service turns it into docindex-ui-action so the open
    Builder refreshes at once instead of waiting for the poll tick)."""
    tools = _dash_tools()
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        user_obj = db.session.get(User, user)
        added = tools._tool_add_card(
            user_obj, {"file_id": fid, "type": "kpi",
                       "title": "K"}, None)
        assert added["ui"]["type"] == "dashboard_changed"
        assert added["ui"]["file_id"] == fid
        cid = added["card"]["id"]
        renamed = tools._tool_update(
            user_obj, {"file_id": fid, "title": "New"}, None)
        assert renamed["ui"]["type"] == "dashboard_changed"
        tweaked = tools._tool_update_card(
            user_obj, {"file_id": fid, "card_id": cid,
                       "title": "K2"}, None)
        assert tweaked["ui"]["type"] == "dashboard_changed"
        attached = tools._tool_attach_file(
            user_obj, {"file_id": fid, "name": "n.txt",
                       "content": "n"}, None)
        assert attached["ui"]["type"] == "dashboard_changed"
        detached = tools._tool_detach_file(
            user_obj, {"file_id": fid,
                       "attachment_id": attached["attached"]["id"]},
            None)
        assert detached["ui"]["type"] == "dashboard_changed"
        removed = tools._tool_delete_card(
            user_obj, {"file_id": fid, "card_id": cid}, None)
        assert removed["ui"]["type"] == "dashboard_changed"
        assert "ui" not in tools._tool_read(
            user_obj, {"file_id": fid}, None)
        assert "ui" not in tools._tool_list(user_obj, {}, None)


def test_builder_toolbar_ai_toggles(app, auth_client, user, enabled):
    """C6: the Builder toolbar carries dashboard-level AI lock/hide
    toggles (per-card Settings drawers cannot host dashboard flags);
    the read-only View toolbar offers none."""
    builder = auth_client.get(
        f"/m/dashboards/{_create(app, user, _drive(app, user))}").data.decode()
    assert "dash-lock-btn" in builder and "dash-hide-btn" in builder
    fid = _create(app, user, _drive(app, user))
    view = auth_client.get(
        f"/m/dashboards/{fid}?mode=view").data.decode()
    assert "dash-lock-btn" not in view and "dash-hide-btn" not in view
    src = _dash_js_src()
    assert "dash-lock-btn" in src and "dash-hide-btn" in src
    assert "ai_lock" in src and "ai_hidden" in src
    assert ".dash-on" in _dash_css()


def test_builder_poll_adopts_structural_edits(app, auth_client, user,
                                              enabled):
    """C6: the status channel carries the checksum, and the Builder poll
    adopts remote structural (AI chat) edits live when it holds no
    unsaved work — run-only changes still merge footers in place."""
    import re
    import shutil
    import subprocess
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    body = auth_client.get(f"/m/dashboards/{fid}/status").get_json()
    assert body["ok"] and body["checksum"]
    src = _dash_js_src()
    assert "structuralSig" in src  # structural fingerprint helper
    assert "dashboard_changed" in src  # push leg: ui event listener
    assert "docindex-ui-action" in src
    match = re.search(
        r"var RUN_OWNED_KEYS = (\[.*?\]);\s*"
        r"(function structuralCard\(card\) \{.*?\n    \})\s*"
        r"(function structuralSig\(doc\) \{.*?\n    \})",
        src, re.S)
    assert match, "structuralSig helpers missing"
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (match.group(0)
             + "\nvar base = {title: 'T', ai_lock: false, ai_hidden: false,"
             + " variables: {}, attachments: [],"
             + " cards: [{id: 'c1', type: 'kpi', title: 'K', prompt: 'P',"
             + " result: null, status: 'idle', ranAt: null}]};"
             + "\nvar runOnly = JSON.parse(JSON.stringify(base));"
             + "runOnly.cards[0].result = {text: '3'};"
             + "runOnly.cards[0].status = 'done';"
             + "runOnly.cards[0].ranAt = '2026-01-01T00:00:00';"
             + "\nvar structural = JSON.parse(JSON.stringify(base));"
             + "structural.cards[0].prompt = 'Q';"
             + "\nvar added = JSON.parse(JSON.stringify(base));"
             + "added.cards.push({id: 'c2', type: 'kpi'});"
             + "\nvar regOnly = JSON.parse(JSON.stringify(base));"
             + "regOnly.createdFileIds = [9];"
             + "regOnly.cards[0].runId = '1:abc';"
             + "\nconsole.log(JSON.stringify(["
             + "structuralSig(base) === structuralSig(runOnly),"
             + "structuralSig(base) !== structuralSig(structural),"
             + "structuralSig(base) !== structuralSig(added),"
             + "structuralSig(base) === structuralSig(regOnly)]));")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == [True, True, True, True]


def test_builder_poll_defers_while_editing():
    """Fix round 1: the wholesale adopt never fires while the popup card
    editor or a Settings drawer is open — the shared refresh leg gates on
    builderBusy, and drawer close flushes at once (toggle capture)."""
    import re
    import shutil
    import subprocess
    src = _dash_js_src()
    assert "builderBusy" in src
    gate = re.search(r"function refreshFromServer\(S\) \{(.*?)\n    \}",
                     src, re.S)
    assert gate, "refreshFromServer missing"
    assert "builderBusy(S)" in gate.group(1)
    flush = re.search(
        r"addEventListener\(\"toggle\", function \(e\) \{(.*?)\n"
        r"        \}, \{ signal: ac\.signal, capture: true \}\);",
        src, re.S)
    assert flush, "drawer-close flush listener missing"
    assert "dash-settings" in flush.group(1)
    assert "refreshFromServer(S)" in flush.group(1)
    busy = re.search(r"function builderBusy\(S\) \{(.*?)\n    \}", src, re.S)
    assert busy, "builderBusy helper missing"
    if shutil.which("node") is None:
        pytest.skip("node not available")
    probe = (busy.group(0)
             + "\nvar closed = { _edit: null, els: {},"
             + " root: { querySelector: function () { return null; } } };"
             + "\nvar editing = { _edit: { card: 1 }, els: {},"
             + " root: { querySelector: function () { return null; } } };"
             + "\nvar dlgOpen = { _edit: null,"
             + " els: { dlgEdit: { open: true } },"
             + " root: { querySelector: function () { return null; } } };"
             + "\nvar drawerOpen = { _edit: null, els: {}, root: {"
             + " querySelector: function () { return { querySelector:"
             + " function () { return {}; } }; } } };"
             + "\nconsole.log(JSON.stringify(["
             + "builderBusy(closed), builderBusy(editing),"
             + "builderBusy(dlgOpen), builderBusy(drawerOpen)]));")
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == [False, True, True, True]
