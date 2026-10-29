"""Whiteboards module: scenes, API, editor page and AI tools."""

import pytest

from app.extensions import db
from app.models import Drive, ModuleState, StoredFile, User
from modules.whiteboards import board as wb
from modules.whiteboards import tools as wb_tools


@pytest.fixture()
def enabled(app):
    with app.app_context():
        db.session.add(ModuleState(name="whiteboards", enabled=True))
        db.session.commit()


def _drive(app, user_id):
    with app.app_context():
        d = Drive(name="Personal", user_id=user_id, source_path=None)
        db.session.add(d)
        db.session.commit()
        return d.id


def _user(app, user_id):
    with app.app_context():
        return db.session.get(User, user_id)


def _create(app, user_id, drive_id, name="Board", elements=None):
    with app.app_context():
        user = db.session.get(User, user_id)
        drive = db.session.get(Drive, drive_id)
        return wb.create_board(user, drive, name,
                               elements=elements).id


# --- Scene validation -----------------------------------------------------


def test_validate_element_defaults_and_rejects():
    el = wb.validate_element({"type": "rectangle", "x": 10, "y": 20})
    assert el["width"] == 200.0 and el["height"] == 120.0
    assert el["id"] and el["strokeColor"] == "#1e1e1e"
    assert wb.validate_element({"type": "text", "text": "hi"})["fontSize"] == 20.0
    with pytest.raises(ValueError):
        wb.validate_element({"type": "circle"})
    with pytest.raises(ValueError):
        wb.validate_element("just a string")
    with pytest.raises(ValueError):
        wb.validate_element({"type": "rectangle", "width": -5})


def test_arrange_align_and_distribute():
    els = [wb.validate_element({"type": "rectangle", "x": x, "y": 0,
                                "width": 10, "height": 10})
           for x in (0, 50, 100)]
    left, skipped = wb.arrange(els, "align-left")
    assert skipped == 0
    assert [e["x"] for e in left] == [0, 0, 0]
    dist, _ = wb.arrange(els, "distribute-x")
    xs = [e["x"] for e in dist]
    assert xs[0] == 0 and xs[-1] == 100
    assert xs[1] == pytest.approx(50)
    with pytest.raises(ValueError):
        wb.arrange(els, "explode")
    with pytest.raises(ValueError):
        wb.arrange(els[:1], "align-left")


def test_extract_text(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Plan", elements=[
        {"type": "text", "x": 0, "y": 0, "text": "hello board"},
        {"type": "rectangle", "x": 10, "y": 10}])
    with app.app_context():
        text = wb.extract(db.session.get(StoredFile, fid))
    assert "hello board" in text and "Plan" in text


# --- Storage ----------------------------------------------------------------


def test_create_and_list_boards(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Ideas")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert stored.extension == "wboard"
        boards = wb.list_boards(user)
        assert [b.id for b in boards] == [fid]
        loaded = wb.load(stored)
        assert loaded["title"] == "Ideas"
        assert loaded["elements"] == []


def test_save_board_round_trip_and_history(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        scene = wb.load(stored)
        scene["elements"] = [wb.validate_element(
            {"type": "text", "x": 5, "y": 5, "text": "abc"})]
        wb.save_board(stored, scene, note="test")
        assert wb.load(db.session.get(StoredFile, fid))["elements"][0]["text"] == "abc"
        assert db.session.get(StoredFile, fid).versions, "expected a history snapshot"


def _add_conn(client):
    return client.post("/settings/ai/add", data={
        "name": "Test Conn", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test", "model": "gpt-4o-mini", "vision_model": "",
        "is_active": "on"}, follow_redirects=True)


def _chat_capture(auth_client, payload):
    from unittest.mock import patch
    captured = {}

    def fake_completion(*args, **kwargs):
        captured["messages"] = kwargs.get("messages") or args[0]
        return {"role": "assistant", "content": "done"}

    with patch("app.services.ai_service.chat_completion",
               side_effect=fake_completion):
        resp = auth_client.post("/ai/chat", json=payload)
    assert resp.status_code == 200
    return captured["messages"]


def test_open_board_context_names_whiteboard(auth_client, app, user, enabled):
    """An open board feeds the chat like an open file: the agent sees a
    system note naming the whiteboard (no explicit @here needed)."""
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Roadmap", elements=[
        {"type": "text", "x": 0, "y": 0, "text": "milestone one"}])
    _add_conn(auth_client)
    messages = _chat_capture(auth_client, {
        "message": "summarize this board", "context_file_id": fid})
    notes = [m for m in messages
             if m["role"] == "system" and "currently has" in m["content"]]
    assert notes
    assert "whiteboard" in notes[0]["content"]
    assert f"[id {fid}]" in notes[0]["content"]
    assert "Roadmap" in notes[0]["content"]


def test_locked_board_context_warns(auth_client, app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Locked plan")
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        scene = wb.load(stored)
        scene["ai_lock"] = True
        wb.save_board(stored, scene, skip_snapshot=True)
    _add_conn(auth_client)
    messages = _chat_capture(auth_client, {
        "message": "edit this board", "context_file_id": fid})
    notes = [m for m in messages
             if m["role"] == "system" and "currently has" in m["content"]]
    assert notes
    assert "LOCKED" in notes[0]["content"]


def test_board_attachment_brief(app, user, enabled):
    from app.services import file_service
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Brief", elements=[
        {"type": "text", "x": 0, "y": 0, "text": "hello board"}])
    with app.app_context():
        desc = file_service.describe_for_chat(
            db.session.get(StoredFile, fid))
    assert desc["kind"] == "whiteboard"
    assert "hello board" in (desc.get("head") or "")


def test_auto_place_flows_coordinateless_elements():
    raw = [{"type": "rectangle"}, {"type": "ellipse"},
           {"type": "text", "text": "hi"}]
    clean = [wb.validate_element(e) for e in raw]
    idx = {i for i, e in enumerate(raw) if wb._wants_auto(e)}
    assert idx == {0, 1, 2}
    placed = wb.auto_place(clean, idx)
    assert placed == 3
    pos = {(e["x"], e["y"]) for e in clean}
    assert len(pos) == 3  # spread out, not piled at 0,0
    assert all(x >= 40 and y >= 60 for x, y in pos)


def test_auto_place_respects_explicit_coords_and_content():
    raw = [{"type": "rectangle", "x": 0, "y": 0},
           {"type": "rectangle"}]
    clean = [wb.validate_element(e) for e in raw]
    idx = {i for i, e in enumerate(raw) if wb._wants_auto(e)}
    assert idx == {1}
    existing = [{"x": 0, "y": 0, "width": 50, "height": 400}]
    placed = wb.auto_place(clean, idx, existing)
    assert placed == 1
    assert (clean[0]["x"], clean[0]["y"]) == (0, 0)  # explicit kept
    assert clean[1]["y"] >= 460  # flows below the existing content


def test_tool_add_auto_places_and_reports(app, user, enabled):
    _drive(app, user)
    user_obj = _user(app, user)
    with app.app_context():
        created = wb_tools._tool_create(
            user_obj, {"name": "Flow",
                       "elements": [{"type": "rectangle"}]}, None)
        fid = created["created"]["id"]
        res = wb_tools._tool_add(
            user_obj, {"file_id": fid, "elements": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b", "x": 5, "y": 5}]}, None)
    assert res["ok"] is True
    assert res.get("auto_placed") == 1  # only the coord-less one
    assert "x/y" in res.get("note", "")
    with app.app_context():
        from app.models import StoredFile
        scene = wb.load(db.session.get(StoredFile, fid))
    texts = [e for e in scene["elements"] if e.get("type") == "text"]
    assert len(texts) == 2
    assert (texts[0]["x"], texts[0]["y"]) != (0, 0)
    assert (texts[1]["x"], texts[1]["y"]) == (5, 5)


def test_text_on_shape_becomes_bound_label(app, user, enabled):
    _drive(app, user)
    user_obj = _user(app, user)
    with app.app_context():
        created = wb_tools._tool_create(
            user_obj, {"name": "Labeled", "elements": [
                {"type": "rectangle", "x": 0, "y": 0, "text": "Step 1"}]},
            None)
        fid = created["created"]["id"]
        res = wb_tools._tool_add(
            user_obj, {"file_id": fid, "elements": [
                {"type": "rectangle", "x": 300, "y": 0,
                 "text": "Step 2"}]}, None)
    assert res["ok"] is True
    assert res.get("labels") == 1
    with app.app_context():
        from app.models import StoredFile
        scene = wb.load(db.session.get(StoredFile, fid))
    shapes = [e for e in scene["elements"] if e["type"] == "rectangle"]
    labels = [e for e in scene["elements"] if e["type"] == "text"]
    assert all("text" not in sh for sh in shapes)  # no fake text on shapes
    assert len(labels) == 2
    by_shape = {sh["id"] for sh in shapes}
    assert {lb["containerId"] for lb in labels} <= by_shape
    assert {lb["text"] for lb in labels} == {"Step 1", "Step 2"}
    label = next(lb for lb in labels if lb["text"] == "Step 1")
    shape = next(sh for sh in shapes if sh["id"] == label["containerId"])
    assert shape["x"] <= label["x"] <= shape["x"] + shape["width"]


def test_update_shape_text_reuses_label(app, user, enabled):
    _drive(app, user)
    user_obj = _user(app, user)
    with app.app_context():
        created = wb_tools._tool_create(
            user_obj, {"name": "Relabel", "elements": [
                {"type": "rectangle", "x": 10, "y": 10}]}, None)
        fid = created["created"]["id"]
        eid = wb_tools._tool_context(
            user_obj, {"file_id": fid}, None)["digest"][0].split()[0]
        r1 = wb_tools._tool_update(
            user_obj, {"file_id": fid, "element_id": eid,
                       "patch": {"text": "v1"}}, None)
        assert r1["ok"] is True and r1.get("labels") == 1
        r2 = wb_tools._tool_update(
            user_obj, {"file_id": fid, "element_id": eid,
                       "patch": {"text": "v2"}}, None)
        assert r2["ok"] is True
    with app.app_context():
        from app.models import StoredFile
        scene = wb.load(db.session.get(StoredFile, fid))
    labels = [e for e in scene["elements"]
              if e.get("containerId") == eid]
    assert len(labels) == 1  # updated in place, not duplicated
    assert labels[0]["text"] == "v2"


def test_link_and_roundness_survive_round_trip(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Links", elements=[
        {"type": "rectangle", "x": 0, "y": 0, "link": "https://x.test/a",
         "roundness": {"type": 1}}])
    with app.app_context():
        scene = wb.load(db.session.get(StoredFile, fid))
    el = scene["elements"][0]
    assert el["link"] == "https://x.test/a"
    assert el["roundness"] == {"type": 1}


def test_locked_elements_rejected(app, user, enabled):
    _drive(app, user)
    user_obj = _user(app, user)
    with app.app_context():
        created = wb_tools._tool_create(
            user_obj, {"name": "Locked", "elements": [
                {"type": "rectangle", "x": 0, "y": 0, "locked": True},
                {"type": "rectangle", "x": 50, "y": 50}]}, None)
        fid = created["created"]["id"]
        ctx = wb_tools._tool_context(user_obj, {"file_id": fid}, None)
        locked_id = next(
            lid for lid in [line.split()[0] for line in ctx["digest"]]
            if any(e["id"] == lid and e.get("locked")
                   for e in wb.load(db.session.get(StoredFile, fid))["elements"]))
        err = wb_tools._tool_update(
            user_obj, {"file_id": fid, "element_id": locked_id,
                       "patch": {"x": 999}}, None)
        assert "locked" in err["error"]
        err2 = wb_tools._tool_delete(
            user_obj, {"file_id": fid, "element_ids": [locked_id]},
            None)
        assert "locked" in err2["error"]
        with app.app_context():
            assert len(wb.load(db.session.get(StoredFile, fid))["elements"]) == 2


def test_arrange_skips_locked_and_moves_labels(app, user, enabled):
    els = [wb.validate_element({"type": "rectangle", "x": x, "y": 0,
                                "width": 10, "height": 10,
                                "locked": (x == 0)})
           for x in (0, 50, 100)]
    mid = els[1]
    label = wb.make_label(mid, "L")
    els.append(label)
    out, skipped = wb.arrange(els, "align-top")
    assert skipped == 1
    by_id = {e["id"]: e for e in out}
    assert by_id[els[0]["id"]]["y"] == 0  # locked stays
    assert by_id[mid["id"]]["y"] == 0
    assert by_id[label["id"]]["containerId"] == mid["id"]


def test_delete_reports_count(app, user, enabled):
    _drive(app, user)
    user_obj = _user(app, user)
    with app.app_context():
        created = wb_tools._tool_create(
            user_obj, {"name": "Del", "elements": [
                {"type": "rectangle", "x": 0, "y": 0},
                {"type": "text", "x": 0, "y": 0, "text": "t"}]}, None)
        fid = created["created"]["id"]
        res = wb_tools._tool_delete(
            user_obj, {"file_id": fid, "element_ids": ["zzz"]}, None)
        assert "error" in res
        ids = [line.split()[0] for line in
               wb_tools._tool_context(user_obj, {"file_id": fid},
                                      None)["digest"]]
        res2 = wb_tools._tool_delete(
            user_obj, {"file_id": fid, "element_ids": ids}, None)
        assert res2.get("deleted") == 2


def test_ai_lock_and_hide(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id)
    user_obj = _user(app, user)
    with app.app_context():
        assert wb_tools._tool_add(
            user_obj, {"file_id": fid, "elements": [
                {"type": "text", "x": 0, "y": 0, "text": "v1"}]},
            None)["ok"] is True
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        scene = wb.load(stored)
        scene["ai_lock"] = True
        wb.save_board(stored, scene, skip_snapshot=True)
    with app.app_context():
        assert "LOCKED" in wb_tools._tool_add(
            user_obj, {"file_id": fid, "elements": [
                {"type": "text", "x": 0, "y": 0, "text": "v2"}]},
            None)["error"]
        # Reads still work on locked boards.
        assert wb_tools._tool_get_scene(
            user_obj, {"file_id": fid}, None)["scene"]["elements"]
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        scene = wb.load(stored)
        scene.update({"ai_lock": False, "ai_hidden": True})
        wb.save_board(stored, scene, skip_snapshot=True)
    with app.app_context():
        assert "hidden" in wb_tools._tool_get_scene(
            user_obj, {"file_id": fid}, None)["error"]
        listed = wb_tools._tool_list(user_obj, {}, None)
    assert listed["total"] == 0


# --- Routes -----------------------------------------------------------------


def test_delete_moves_board_to_trash(auth_client, app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Delete me")
    resp = auth_client.post(f"/m/whiteboards/{fid}/delete",
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(StoredFile, fid).deleted_at is not None
        ids = [b.id for b in wb.list_boards(user)]
    assert fid not in ids


def test_board_pages_and_scene_api(auth_client, app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "API board")
    assert auth_client.get("/m/whiteboards/").status_code == 200
    resp = auth_client.get(f"/m/whiteboards/{fid}")
    assert resp.status_code == 200
    assert b"API board" in resp.data
    assert b"wb-root" in resp.data
    scene = {"elements": [wb.validate_element(
        {"type": "rectangle", "x": 1, "y": 2})]}
    put = auth_client.put(f"/m/whiteboards/api/{fid}/scene", json=scene)
    assert put.status_code == 200
    assert put.get_json()["elements"] == 1
    got = auth_client.get(f"/m/whiteboards/api/{fid}/scene").get_json()
    assert got["scene"]["elements"][0]["x"] == 1
    assert got["version"]
    bad = auth_client.put(f"/m/whiteboards/api/{fid}/scene",
                          json={"elements": [{"type": "nope"}]})
    assert bad.status_code == 400


def test_create_route_and_lock_toggle(auth_client, app, user, enabled):
    drive_id = _drive(app, user)
    resp = auth_client.post("/m/whiteboards/create",
                            data={"name": "Route board",
                                  "drive_id": drive_id},
                            follow_redirects=False)
    assert resp.status_code == 302
    fid = int(resp.headers["Location"].rsplit("/", 1)[-1])
    auth_client.post(f"/m/whiteboards/{fid}/lock")
    with app.app_context():
        assert wb.access_flags(db.session.get(StoredFile, fid))[0] is True
    auth_client.post(f"/m/whiteboards/{fid}/unlock")
    with app.app_context():
        assert wb.access_flags(db.session.get(StoredFile, fid))[0] is False


# --- AI tools ---------------------------------------------------------------


def test_ai_tools_end_to_end(app, user, enabled):
    _drive(app, user)
    user_obj = _user(app, user)
    with app.app_context():
        created = wb_tools._tool_create(
            user_obj, {"name": "AI board", "elements": [
                {"type": "rectangle", "x": 0, "y": 0},
                {"type": "rectangle", "x": 50, "y": 50}]}, None)
    fid = created["created"]["id"]
    with app.app_context():
        ctx = wb_tools._tool_context(user_obj, {"file_id": fid}, None)
        assert ctx["elements"] == 2 and "rectangle" in ctx["digest"][0]
        eid = ctx["digest"][0].split()[0]
        upd = wb_tools._tool_update(
            user_obj, {"file_id": fid, "patch": {"x": 42},
                       "element_id": eid}, None)
        assert upd["ok"] is True
        arr = wb_tools._tool_arrange(
            user_obj, {"file_id": fid, "mode": "align-left"}, None)
        assert arr["ok"] is True
        denied = wb_tools._tool_clear(user_obj, {"file_id": fid}, None)
        assert "confirm" in denied["error"]
        cleared = wb_tools._tool_clear(
            user_obj, {"file_id": fid, "confirm": True}, None)
        assert cleared["ok"] is True and cleared["elements"] == 0
        assert wb_tools._tool_delete(
            user_obj, {"file_id": fid, "element_ids": ["zzz"]},
            None)["error"]
        assert "No element" in wb_tools._tool_update(
            user_obj, {"file_id": fid, "element_id": "zzz",
                       "patch": {"x": 1}}, None)["error"]
        assert wb_tools._tool_create(
            user_obj, {"name": "Bad", "elements": [{"type": "nope"}]},
            None)["error"]
