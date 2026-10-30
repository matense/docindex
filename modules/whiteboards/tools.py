"""AI tools contributed by the whiteboards module (namespaced whiteboards.*).

Mutations go through board.save_board_ai: the first edit of a board within
one AI answer snapshots the pre-edit state; later edits in the same run
just write through — one answer, one restorable history block.
"""

from flask import g, has_app_context

from app.extensions import db
from app.models import Drive
from app.services import search_service
from app.services.agent_service import register_tool

from . import board as board_service


def _schema(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required)}}}


ELEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable id (omit for new elements — one is assigned)."},
        "type": {"type": "string", "description": "rectangle | ellipse | diamond | arrow | line | text."},
        "x": {"type": "number", "description": "Left edge (optional - omitted with y = auto-placed in a flow)."},
      "y": {"type": "number", "description": "Top edge (optional - omitted with x = auto-placed in a flow)."},
        "width": {"type": "number"}, "height": {"type": "number"},
        "text": {"type": "string", "description": "Text content. On shapes (rectangle/ellipse/diamond/arrow/line) a centered label is auto-created and bound to the shape."},
        "fontSize": {"type": "number"},
        "strokeColor": {"type": "string", "description": "CSS color, e.g. #1e1e1e."},
        "backgroundColor": {"type": "string"},
        "strokeWidth": {"type": "number"},
        "angle": {"type": "number"},
        "locked": {"type": "boolean"},
      "isDeleted": {"type": "boolean"},
      "link": {"type": "string", "description": "Optional hyperlink kept on the shape."},
        "groupIds": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["type"],
}


def _owned_board(user, file_id):
    try:
        fid = int(file_id)
    except (TypeError, ValueError):
        return None, {"error": f"Board {file_id!r} not found."}
    stored = board_service.get_board(fid, user.id)
    if stored is None:
        return None, {"error": f"Board {file_id} not found."}
    err = board_service.ai_error(stored)
    if err:
        return None, {"error": err}
    return stored, None


def _summary(stored):
    try:
        scene = board_service.load(stored)
    except (ValueError, OSError):
        return {"id": stored.id, "name": stored.name, "unreadable": True}
    ai_lock, ai_hidden = board_service.access_flags(stored)
    return {
        "id": stored.id, "name": stored.name,
        "drive": stored.drive.name if stored.drive else None,
        "folder": stored.folder.name if stored.folder else None,
        "elements": len(scene.get("elements", [])),
        "ai_lock": ai_lock,
        "updated_at": (stored.updated_at.isoformat()
                       if stored.updated_at else None),
    }


def _save_ai(stored, scene, note):
    """One history snapshot per AI answer, then write-through saves."""
    first_in_run = True
    if has_app_context():
        snapped = getattr(g, "_wb_ai_snapshotted", None)
        if snapped is None:
            snapped = set()
            g._wb_ai_snapshotted = snapped
        first_in_run = stored.id not in snapped
        snapped.add(stored.id)
    board_service.save_board(stored, scene, note=note, source="ai",
                             skip_snapshot=not first_in_run)


def _resolve_drive(user, args, drive):
    if args.get("drive_id"):
        target = db.session.get(Drive, int(args["drive_id"]))
        if target is None or target.user_id != user.id:
            return None, "Drive not found."
        if target.is_readonly:
            return None, "That drive is synced (read-only) — pick a writable drive."
        return target, None
    if drive is not None:
        candidates = list(drive) if isinstance(drive, (list, tuple)) else [drive]
        target = next((d for d in candidates if not d.is_readonly), None)
        if target is None and candidates:
            return None, ("All drives in context are synced (read-only) — "
                           "pick a writable drive with drive_id.")
        if target is not None:
            return target, None
    target = (Drive.query.filter_by(user_id=user.id, source_path=None)
              .order_by(Drive.id).first())
    if target is None:
        return None, "No writable drive found."
    return target, None


def _tool_list(user, args, drive):
    boards = board_service.list_boards(user.id)
    drive_ids = search_service.drive_id_list(drive)
    if drive_ids is not None:
        boards = [b for b in boards if b.drive_id in drive_ids]
    boards = [b for b in boards
              if not board_service.ai_error(b)]
    return {"total": len(boards),
            "boards": [_summary(b) for b in boards[:100]]}


def _tool_create(user, args, drive):
    name = (args.get("name") or "").strip() or "Untitled board"
    target, error = _resolve_drive(user, args, drive)
    if error:
        return {"error": error}
    elements = args.get("elements") or []
    if not isinstance(elements, list):
        return {"error": "'elements' must be a list of element objects."}
    try:
        stored = board_service.create_board(user, target, name,
                                            elements=elements)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"created": _summary(stored)}


def _tool_get_scene(user, args, drive):
    stored, error = _owned_board(user, args.get("file_id"))
    if error:
        return error
    try:
        scene = board_service.load(stored)
    except (ValueError, OSError) as exc:
        return {"error": f"Board file is unreadable: {exc}"}
    return {"scene": scene,
            "version": board_service.scene_version(stored)}


def _tool_context(user, args, drive):
    """Compact board digest so the model can orient before editing."""
    stored, error = _owned_board(user, args.get("file_id"))
    if error:
        return error
    try:
        scene = board_service.load(stored)
    except (ValueError, OSError) as exc:
        return {"error": f"Board file is unreadable: {exc}"}
    lines = []
    for el in scene.get("elements", [])[:300]:
        bit = f"{el['id']} {el['type']} @({round(el['x'])},{round(el['y'])})"
        if el.get("text"):
            bit += f" {el['text'][:80]!r}"
        else:
            bit += f" {round(el.get('width', 0))}x{round(el.get('height', 0))}"
        lines.append(bit)
    return {"id": stored.id, "title": scene.get("title"),
            "elements": len(scene.get("elements", [])),
            "digest": lines,
            "version": board_service.scene_version(stored)}


def _scene_locked_check(stored, ids):
    """Raise if any targeted element is user-locked on the board."""
    try:
        scene = board_service.load(stored)
    except (ValueError, OSError) as exc:
        raise ValueError(f"Board file is unreadable: {exc}") from exc
    locked = [e.get("id") for e in scene.get("elements", [])
              if e.get("id") in set(ids) and e.get("locked")]
    if locked:
        raise ValueError(f"Refusing to edit locked element(s): "
                         f"{', '.join(locked)}. Unlock them on the board "
                         f"first.")


def _mutate(user, args, drive, op, note):
    stored, error = _owned_board(user, args.get("file_id"))
    if error:
        return error
    locked = board_service.ai_error(stored, write=True)
    if locked:
        return {"error": locked}
    try:
        scene = board_service.load(stored)
    except (ValueError, OSError) as exc:
        return {"error": f"Board file is unreadable: {exc}"}
    try:
        scene = op(scene)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        _save_ai(stored, scene, note)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, "id": stored.id,
            "elements": len(scene.get("elements", [])),
            "version": board_service.scene_version(stored)}


def _tool_add(user, args, drive):
    elements = args.get("elements")
    if not isinstance(elements, list) or not elements:
        return {"error": "'elements' must be a non-empty list."}
    raw = list(elements)
    auto_idx = {i for i, e in enumerate(raw)
                if board_service._wants_auto(e)}

    def op(scene):
        clean = [board_service.validate_element(e) for e in raw]
        # No coordinates at all: flow into free space instead of
        # piling everything at 0,0 (weak models omit geometry).
        op.placed = board_service.auto_place(
            clean, auto_idx, scene.get("elements", []))
        # Text on a shape becomes a bound centered label (shapes cannot
        # carry text themselves - it would otherwise be silently lost).
        labels = board_service.attach_labels(clean, raw)
        op.labels = len(labels)
        scene["elements"] = scene.get("elements", []) + clean + labels
        return scene

    op.placed = 0
    op.labels = 0
    result = _mutate(user, args, drive, op, "AI added elements")
    if result.get("ok"):
        bits = []
        if op.placed:
            result["auto_placed"] = op.placed
            bits.append(f"{op.placed} element(s) had no coordinates and "
                        "were flowed into free space - pass x/y to place "
                        "them exactly")
        if op.labels:
            result["labels"] = op.labels
            bits.append(f"{op.labels} text label(s) auto-bound to their "
                        "shapes")
        if bits:
            result["note"] = ". ".join(bits) + "."
    return result


def _tool_update(user, args, drive):
    eid = args.get("element_id")
    patch = args.get("patch")
    if not eid or not isinstance(patch, dict):
        return {"error": "Give element_id and a patch object."}
    locked_chk, locked_err = _owned_board(user, args.get("file_id"))
    if locked_err:
        return locked_err
    try:
        _scene_locked_check(locked_chk, [eid])
    except ValueError as exc:
        return {"error": str(exc)}
    allowed = {"x", "y", "width", "height", "angle", "text", "fontSize",
               "fontFamily", "textAlign", "verticalAlign", "strokeColor",
               "backgroundColor", "fillStyle", "strokeWidth", "strokeStyle",
               "roughness", "opacity", "isDeleted", "locked", "groupIds",
               "startArrowhead", "endArrowhead", "points", "link"}

    label_text = patch.get("text") if isinstance(patch, dict) else None
    if isinstance(label_text, str) and not label_text.strip():
        label_text = None

    def op(scene):
        found = False
        out = []
        label_target = None
        for el in scene.get("elements", []):
            if el.get("id") == eid:
                found = True
                merged = dict(el)
                for k, v in patch.items():
                    if k in allowed:
                        merged[k] = v
                if label_text and merged.get("type") != "text":
                    # Text on a shape becomes its bound label (shapes
                    # cannot carry text themselves - it would be dropped).
                    label_target = merged
                out.append(board_service.validate_element(merged))
            else:
                out.append(el)
        if not found:
            raise ValueError(f"No element {eid!r} on this board.")
        labels = 0
        if label_target is not None:
            existing = next(
                (x for x in out
                 if x.get("type") == "text"
                 and x.get("containerId") == eid), None)
            if existing is None:
                out.append(board_service.make_label(
                    label_target, label_text,
                    patch.get("fontSize", 20)))
            else:
                existing["text"] = str(label_text)[:20000]
            labels = 1
            op.labels = labels
        scene["elements"] = out
        return scene

    op.labels = 0
    result = _mutate(user, args, drive, op, "AI updated an element")
    if result.get("ok") and op.labels:
        result["labels"] = op.labels
        result["note"] = ("Text applied as a label bound to the shape "
                          "(shapes cannot carry text themselves).")
    return result


def _tool_delete(user, args, drive):
    ids = args.get("element_ids") or ([args["element_id"]]
                                      if args.get("element_id") else [])
    if not ids:
        return {"error": "Give element_id or element_ids."}
    ids = set(ids)

    locked_chk, locked_err = _owned_board(user, args.get("file_id"))
    if locked_err:
        return locked_err
    try:
        scene_chk = board_service.load(locked_chk)
    except (ValueError, OSError) as exc:
        return {"error": f"Board file is unreadable: {exc}"}
    locked_ids = sorted(e.get("id") for e in scene_chk.get("elements", [])
                        if e.get("id") in ids and e.get("locked"))
    if locked_ids:
        return {"error": f"Refusing to delete locked element(s): "
                         f"{', '.join(locked_ids)}. Unlock them on the "
                         f"board first."}

    def op(scene):
        before = len(scene.get("elements", []))
        scene["elements"] = [e for e in scene.get("elements", [])
                             if e.get("id") not in ids]
        removed = before - len(scene["elements"])
        if removed == 0:
            raise ValueError("None of those element ids are on this board.")
        op.removed = removed
        return scene

    op.removed = 0
    result = _mutate(user, args, drive, op, "AI deleted elements")
    if result.get("ok"):
        result["deleted"] = op.removed
    return result


def _tool_arrange(user, args, drive):
    mode = args.get("mode")
    ids = args.get("element_ids")

    def op(scene):
        arranged, skipped = board_service.arrange(
            scene.get("elements", []), mode, ids)
        scene["elements"] = arranged
        op.skipped = skipped
        return scene

    op.skipped = 0
    result = _mutate(user, args, drive, op, f"AI arranged ({mode})")
    if result.get("ok") and op.skipped:
        result["skipped_locked"] = op.skipped
        result["note"] = (f"{op.skipped} locked element(s) left in place.")
    return result


def _tool_clear(user, args, drive):
    if args.get("confirm") is not True:
        return {"error": "Clearing removes every element — pass confirm:true to proceed."}

    def op(scene):
        scene["elements"] = []
        return scene

    return _mutate(user, args, drive, op, "AI cleared the board")


_TOOLS = [
    ("list", "List the user's whiteboards (id, drive, element count).",
     {"query": {"type": "string"}}, ()),
    ("create", "Create a whiteboard, optionally with initial elements.",
     {"name": {"type": "string"},
      "drive_id": {"type": "integer"},
      "elements": {"type": "array", "items": ELEMENT_SCHEMA}}, ()),
    ("get_scene", "Load a board's full scene (elements + layout).",
     {"file_id": {"type": "integer"}}, ("file_id",)),
    ("board_context", "Compact digest of a board (one line per element) to orient before editing.",
     {"file_id": {"type": "integer"}}, ("file_id",)),
    ("add_elements", "Append validated elements (shapes/text/arrows) to a board. "
     "Coordinates are optional: elements without x AND y are flowed into free space. "
     "Example: {\"type\": \"rectangle\", \"x\": 100, \"y\": 80, \"width\": 220, \"height\": 90, \"text\": \"Step 1\"} "
     "(text goes in \"text\" for text elements).",
     {"file_id": {"type": "integer"},
      "elements": {"type": "array", "items": ELEMENT_SCHEMA}},
     ("file_id", "elements")),
    ("update_element", "Patch one element by id (position, size, text, colors…).",
     {"file_id": {"type": "integer"}, "element_id": {"type": "string"},
      "patch": {"type": "object"}}, ("file_id", "element_id", "patch")),
    ("delete_elements", "Remove elements by id.",
     {"file_id": {"type": "integer"},
      "element_id": {"type": "string"},
      "element_ids": {"type": "array", "items": {"type": "string"}}},
     ("file_id",)),
    ("arrange", "Align/distribute elements: align-left|center-x|right, align-top|center-y|bottom, distribute-x|y.",
     {"file_id": {"type": "integer"}, "mode": {"type": "string"},
      "element_ids": {"type": "array", "items": {"type": "string"}}},
     ("file_id", "mode")),
    ("clear", "Remove ALL elements (requires confirm:true).",
     {"file_id": {"type": "integer"}, "confirm": {"type": "boolean"}},
     ("file_id",)),
]


def _label(name):
    return {"list": "List whiteboards", "create": "Create whiteboard",
            "get_scene": "Read board", "board_context": "Board digest",
            "add_elements": "Draw on board", "update_element": "Edit shape",
            "delete_elements": "Delete shapes", "arrange": "Arrange shapes",
            "clear": "Clear board"}.get(name, name)


_HANDLERS = {
    "list": _tool_list, "create": _tool_create,
    "get_scene": _tool_get_scene, "board_context": _tool_context,
    "add_elements": _tool_add, "update_element": _tool_update,
    "delete_elements": _tool_delete, "arrange": _tool_arrange,
    "clear": _tool_clear,
}

for _name, _desc, _props, _req in _TOOLS:
    register_tool(
        _name,
        _schema(_name, _desc, _props, _req),
        _HANDLERS[_name],
        label="Whiteboard: " + _label(_name),
        module="whiteboards",
    )
