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


def test_bound_elements_preserved_and_wired(app, user, enabled):
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Wired", elements=[
        {"type": "rectangle", "x": 0, "y": 0, "text": "Labeled"}])
    with app.app_context():
        from app.models import StoredFile
        scene = wb.load(db.session.get(StoredFile, fid))
        labels = [e for e in scene["elements"] if e.get("type") == "text"]
        assert len(labels) == 1
        label, shape = labels[0], scene["elements"][0]
        # Back-reference present on the container (needed for live render).
        assert {"id": label["id"], "type": "text"} in shape.get(
            "boundElements", [])
        assert label["containerId"] == shape["id"]
    # Legacy scene: label carries containerId but container lacks the
    # back-reference - validation must heal it.
    raw = {"version": 1, "title": "Old", "elements": [
        {"id": "s", "type": "rectangle", "x": 0, "y": 0,
         "width": 100, "height": 50},
        {"id": "t", "type": "text", "x": 10, "y": 10, "text": "Hi",
         "containerId": "s"}]}
    fixed = wb.validate_scene(raw)
    cont = next(e for e in fixed["elements"] if e["id"] == "s")
    assert {"id": "t", "type": "text"} in cont.get("boundElements", [])


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


def test_board_page_failure_surfaces(auth_client, app, user, enabled):
    """X5: the board shell renders failure affordances server-side.

    Import-failure error box + Retry (mirroring the dashboards
    chart-loader error box: bold title + reason + retry affordance),
    capped-save manual controls, and the toast container the PNG-export
    catch reports into. The built editor bundle must carry the matching
    runtime strings (save cap, PNG toast, load-remote).
    """
    from pathlib import Path

    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Surfaces")
    html = auth_client.get(f"/m/whiteboards/{fid}").data.decode()
    assert "wb-load-error-tpl" in html
    assert "wb-retry" in html
    assert "board editor failed to load" in html
    assert "wb-retry-save" in html
    assert "wb-load-remote" in html
    assert "wb-toast-wrap" in html
    dist = Path("modules/whiteboards/static/dist/wb-editor.js")
    bundle = dist.read_text(encoding="utf-8")
    assert "retry paused" in bundle
    assert "PNG export failed" in bundle
    assert "Loaded remote version" in bundle


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


def test_trashed_whiteboard_not_openable(auth_client, app, user, enabled):
    from app.services import file_service
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Trash me")
    with app.app_context():
        file_service.delete_file(db.session.get(StoredFile, fid))
    with app.app_context():
        assert wb.get_board(fid, user) is None
    assert auth_client.get(f"/m/whiteboards/{fid}").status_code == 404
    assert auth_client.get(
        f"/m/whiteboards/api/{fid}/scene").status_code == 404
    with app.app_context():
        user_obj = db.session.get(User, user)
        assert "not found" in wb_tools._tool_get_scene(
            user_obj, {"file_id": fid}, None)["error"].lower()


def test_trashed_whiteboard_rejects_save_and_ai_edit(
        auth_client, app, user, enabled):
    from app.services import file_service
    drive_id = _drive(app, user)
    fid = _create(app, user, drive_id, "Trash save", elements=[
        {"type": "text", "x": 0, "y": 0, "text": "keep me"}])
    with app.app_context():
        before = wb.load(db.session.get(StoredFile, fid))
    with app.app_context():
        file_service.delete_file(db.session.get(StoredFile, fid))
    # Editor PUT save to a trashed board: clean 404, never 500.
    put = auth_client.put(f"/m/whiteboards/api/{fid}/scene",
                          json={"elements": []})
    assert put.status_code == 404
    # AI edit (write) tool on a trashed board: clean "not found"-style
    # error, never 500 and never a successful write.
    with app.app_context():
        user_obj = db.session.get(User, user)
        res = wb_tools._tool_add(
            user_obj, {"file_id": fid, "elements": [
                {"type": "text", "x": 1, "y": 1, "text": "ai"}]}, None)
    assert "error" in res
    assert "not found" in res["error"].lower()
    # Nothing was written through either path: restore and compare.
    with app.app_context():
        file_service.restore_file(db.session.get(StoredFile, fid))
        after = wb.load(db.session.get(StoredFile, fid))
    assert after == before


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


# --- Type Folders F4: "DocIndex Whiteboards" folder per drive + organize ---


def _f4_wboard_in_drive(app, user_id, drive_id, name="stray",
                        folder_id=None, make_on=None):
    fid = _create(app, user_id, make_on or drive_id, name)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        stored.drive_id = drive_id
        stored.folder_id = folder_id
        db.session.commit()
    return fid


def test_whiteboards_folder_and_routing(app, auth_client, user, enabled):
    from app.models import Folder as _Folder

    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        assert wb.WHITEBOARDS_FOLDER == "DocIndex Whiteboards"
        folder = wb.whiteboards_folder(user_obj, drive)
        assert folder.name == "DocIndex Whiteboards"
        assert folder.parent_id is None
        assert folder.drive_id == drive_id
        # Idempotent — second call returns the same folder.
        assert wb.whiteboards_folder(user_obj, drive).id == folder.id
        synced = Drive(name="RO", user_id=user,
                       source_path="/tmp/f4-ro")
        db.session.add(synced)
        db.session.commit()
        assert wb.whiteboards_folder(user_obj, synced) is None
        assert wb.whiteboards_folder(user_obj, None) is None

    # Drive-root create (the web form only picks a drive) routes into the
    # type folder.
    auth_client.post("/m/whiteboards/create",
                     data={"name": "Fresh", "drive_id": drive_id},
                     follow_redirects=True)
    with app.app_context():
        stored = (StoredFile.query
                  .filter_by(user_id=user, extension="wboard")
                  .order_by(StoredFile.id.desc()).first())
        assert stored.folder is not None
        assert stored.folder.name == "DocIndex Whiteboards"
        assert stored.folder.drive_id == drive_id

    # Explicit folder: respected, never redirected (direct API + AI tool —
    # the web form has no explicit-folder picker by design).
    with app.app_context():
        foreign = _Folder(name="Foreign", user_id=user,
                          drive_id=drive_id, parent_id=None)
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        placed = wb.create_board(user_obj, drive, "Placed",
                                 folder=foreign)
        placed_folder_id = placed.folder_id
    assert placed_folder_id == foreign_id
    with app.app_context():
        user_obj = db.session.get(User, user)
        res = wb_tools._tool_create(
            user_obj, {"name": "AI placed", "folder_id": foreign_id},
            None)
    assert res["created"]["folder"] == "Foreign"

    # Synced drive fails closed, naming the drive.
    with app.app_context():
        user_obj = db.session.get(User, user)
        synced = Drive.query.filter_by(user_id=user, name="RO").one()
        with pytest.raises(ValueError, match="RO"):
            wb.create_board(user_obj, synced, "Nope")


def _f4_setup(app, user):
    """One drive with a stray root .wboard + one in a foreign folder, one
    already inside Whiteboards/sub (left alone), and a synced-drive .wboard
    (skipped). Returns ids dict."""
    from app.models import Folder as _Folder

    drive_id = _drive(app, user)
    with app.app_context():
        from app.models import User as _User

        user_obj = db.session.get(_User, user)
        foreign = _Folder(name="Foreign", user_id=user,
                          drive_id=drive_id, parent_id=None)
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
        wb_root = wb.whiteboards_folder(
            user_obj, db.session.get(Drive, drive_id))
        sub = _Folder(name="Sub", user_id=user, drive_id=drive_id,
                      parent_id=wb_root.id)
        db.session.add(sub)
        db.session.commit()
        sub_id = sub.id
    with app.app_context():
        synced = Drive(name="RO", user_id=user,
                       source_path="/tmp/f4-rosy")
        db.session.add(synced)
        db.session.commit()
        synced_id = synced.id
    stray = _f4_wboard_in_drive(app, user, drive_id, "stray")
    abroad = _f4_wboard_in_drive(app, user, drive_id, "abroad",
                                 folder_id=foreign_id)
    nested = _f4_wboard_in_drive(app, user, drive_id, "nested",
                                 folder_id=sub_id)
    rosy = _f4_wboard_in_drive(app, user, synced_id, "rosy",
                               make_on=drive_id)
    return {"drive_id": drive_id, "foreign_id": foreign_id,
            "stray": stray, "abroad": abroad, "nested": nested,
            "rosy": rosy}


def test_whiteboards_organize_roundtrip_and_revert(app, auth_client, user,
                                                  enabled):
    from app.models import WhiteboardOrganizePlan
    from app.services import file_service

    ids = _f4_setup(app, user)
    data = auth_client.get("/m/whiteboards/organize/plan").get_json()
    assert data["skipped_synced"] == 1
    assert {m["name"] for m in data["moves"]} == {"stray.wboard",
                                                  "abroad.wboard"}
    for move in data["moves"]:
        assert move["to_drive_id"] == ids["drive_id"]
        assert move["to_folder_name"] == "DocIndex Whiteboards"
        assert move["from_drive_id"] == ids["drive_id"]

    # Version history survives the move (normal move path, not a copy).
    with app.app_context():
        stored = db.session.get(StoredFile, ids["stray"])
        file_service.snapshot_version(stored, "test")
        before = len(db.session.get(StoredFile, ids["stray"]).versions)
        assert before == 1

    # List page pins the preview-first affordance, no revert yet.
    html = auth_client.get("/m/whiteboards/").data.decode()
    assert "wb-organize-btn" in html
    assert "nothing moves until you confirm" in html.lower()
    assert "DocIndex Whiteboards" in html
    assert "Revert last organize" not in html

    resp = auth_client.post("/m/whiteboards/organize/apply",
                            follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        for fid in (ids["stray"], ids["abroad"]):
            stored = db.session.get(StoredFile, fid)
            assert stored.folder.name == "DocIndex Whiteboards"
            assert stored.drive_id == ids["drive_id"]
        assert db.session.get(StoredFile, ids["nested"]).folder.name == "Sub"
        assert db.session.get(StoredFile, ids["rosy"]).folder_id is None
        assert len(db.session.get(StoredFile,
                                  ids["stray"]).versions) == before
        plan = WhiteboardOrganizePlan.query.one()
        assert plan.applied is True and plan.reverted is False
    assert auth_client.get("/m/whiteboards/organize/plan").get_json()[
        "moves"] == []
    # After an organize run the list offers the revert affordance.
    assert "Revert last organize" in \
        auth_client.get("/m/whiteboards/").data.decode()

    # Revert moves everything back.
    auth_client.post("/m/whiteboards/organize/revert",
                     follow_redirects=True)
    with app.app_context():
        assert db.session.get(StoredFile, ids["stray"]).folder_id is None
        assert db.session.get(StoredFile,
                              ids["abroad"]).folder_id == ids["foreign_id"]
        assert WhiteboardOrganizePlan.query.one().reverted is True
    resp = auth_client.post("/m/whiteboards/organize/revert",
                            follow_redirects=True)
    assert b"No organize run" in resp.data


# --- Type Folders F4 fix round: fail-closed hardening on folder_id ---


def test_whiteboards_create_tool_folder_errors(app, user, enabled):
    from app.models import Folder as _Folder

    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        foreign = _Folder(name="Foreign", user_id=user,
                          drive_id=drive_id, parent_id=None)
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
        synced = Drive(name="RO", user_id=user,
                       source_path="/tmp/f4-err-ro")
        db.session.add(synced)
        db.session.commit()
        synced_id = synced.id
        synced_folder = _Folder(name="RODir", user_id=user,
                                drive_id=synced_id, parent_id=None)
        db.session.add(synced_folder)
        db.session.commit()
        synced_folder_id = synced_folder.id
        # Second user with their own drive + folder.
        other = User(username="bob", email="bob@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        other_drive = Drive(name="Bob", user_id=other.id)
        db.session.add(other_drive)
        db.session.commit()
        other_folder = _Folder(name="BobDir", user_id=other.id,
                               drive_id=other_drive.id, parent_id=None)
        db.session.add(other_folder)
        db.session.commit()
        other_folder_id = other_folder.id

    with app.app_context():
        user_obj = db.session.get(User, user)
        # Invalid folder id.
        assert wb_tools._tool_create(
            user_obj, {"name": "Bad", "folder_id": "abc"},
            None)["error"] == "Invalid folder id."
        # Unknown folder id.
        assert wb_tools._tool_create(
            user_obj, {"name": "Bad", "folder_id": 999999},
            None)["error"] == "Folder not found."
        # Other user's folder.
        assert wb_tools._tool_create(
            user_obj, {"name": "Bad", "folder_id": other_folder_id},
            None)["error"] == "Folder not found."
        # Folder on a synced (read-only) drive fails closed, naming it.
        err = wb_tools._tool_create(
            user_obj, {"name": "Bad", "folder_id": synced_folder_id},
            None)["error"]
        assert "RO" in err and "read-only" in err
        # Conflicting folder_id + drive_id are rejected, not silently
        # resolved in favor of one side.
        err = wb_tools._tool_create(
            user_obj, {"name": "Bad", "folder_id": foreign_id,
                       "drive_id": synced_id}, None)["error"]
        assert "disagree" in err
        # Matching folder_id + drive_id still works.
        res = wb_tools._tool_create(
            user_obj, {"name": "Agree", "folder_id": foreign_id,
                       "drive_id": drive_id}, None)
        assert res["created"]["folder"] == "Foreign"


def test_whiteboards_create_board_rejects_foreign_folder(app, user,
                                                         enabled):
    from app.models import Folder as _Folder

    drive_id = _drive(app, user)
    with app.app_context():
        user_obj = db.session.get(User, user)
        drive = db.session.get(Drive, drive_id)
        other_drive = Drive(name="Other", user_id=user)
        db.session.add(other_drive)
        db.session.commit()
        abroad = _Folder(name="Abroad", user_id=user,
                         drive_id=other_drive.id, parent_id=None)
        db.session.add(abroad)
        db.session.commit()
        with pytest.raises(ValueError, match="target drive"):
            wb.create_board(user_obj, drive, "Cross",
                            folder=abroad)
        other = User(username="carol", email="carol@example.com")
        other.set_password("password123")
        db.session.add(other)
        db.session.commit()
        not_mine = _Folder(name="NotMine", user_id=other.id,
                           drive_id=drive_id, parent_id=None)
        db.session.add(not_mine)
        db.session.commit()
        with pytest.raises(ValueError, match="target drive"):
            wb.create_board(user_obj, drive, "Theirs",
                            folder=not_mine)
        # Nothing was created by the rejected calls.
        assert StoredFile.query.filter_by(
            user_id=user, extension="wboard").count() == 0


def test_whiteboards_shared_drive_roots_at_drive_root(app, user, enabled,
                                                      tmp_path):
    with app.app_context():
        user_obj = db.session.get(User, user)
        shared = Drive(name="Shared", user_id=user,
                       source_path=str(tmp_path), sync_mode="shared")
        db.session.add(shared)
        db.session.commit()
        assert shared.is_synced and not shared.is_readonly
        # No type folder may be created on synced drives — even writable
        # shared ones — so the helper stays None...
        assert wb.whiteboards_folder(user_obj, shared) is None
        # ...and creation keeps its previous drive-root placement.
        stored = wb.create_board(user_obj, shared, "Shared board")
        assert stored.folder_id is None
        assert stored.drive_id == shared.id
        assert wb.get_board(stored.id, user) is not None
