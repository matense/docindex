"""Dashboards module: document model, attachments, versioning (Task 2)."""

import importlib.util
import io
import os
import sys

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import Drive, ModuleState, StoredFile, User
from app.services import indexing_service

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
