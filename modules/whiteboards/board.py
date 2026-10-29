"""Whiteboard scenes: validated Excalidraw element lists stored as .wboard files.

A scene is plain JSON the Excalidraw editor loads verbatim::

    {"version": 1, "title": "...", "elements": [element, ...],
     "appState": {"viewBackgroundColor": "#ffffff"},
     "ai_lock": false, "ai_hidden": false}

Only a hand-picked, shape-safe subset of the Excalidraw element model is
accepted (the model can position shapes/text/arrows, not corrupt the file).
The editor tolerates the rest by applying its own defaults.
"""

import json
import math
import os
import uuid
from io import BytesIO

from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import StoredFile, utcnow
from app.services import file_service, indexing_service

EXTENSION = "wboard"
MIME_TYPE = "application/json"
SCENE_VERSION = 1

ELEMENT_TYPES = {"rectangle", "ellipse", "diamond", "arrow", "line", "text"}

_STROKE_STYLES = {"solid", "dashed", "dotted"}
_FILL_STYLES = {"solid", "hachure", "cross-hatch", "none"}
_ALIGNMENTS = {"left", "center", "right"}
_VALIGNMENTS = {"top", "middle", "bottom"}
_ARROWHEADS = {None, "arrow", "bar", "dot", "triangle", "diamond"}

_MAX_TEXT = 20000
_MAX_ELEMENTS = 2000


def _num(value, default=0.0):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(n):
        return default
    return n


def _str(value, default="", limit=64):
    if value is None:
        return default
    s = str(value)[:limit]
    return s


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    return default


def _clean_id(value):
    if isinstance(value, str) and value.strip():
        return value.strip()[:64]
    return uuid.uuid4().hex[:12]


def validate_element(raw):
    """Coerce one raw dict into a safe Excalidraw element. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("Each element must be an object like "
                         '{"type": "rectangle", "x": 100, "y": 100}.')
    etype = raw.get("type")
    if etype not in ELEMENT_TYPES:
        raise ValueError(f"Unsupported element type {etype!r} — use one of: "
                         + ", ".join(sorted(ELEMENT_TYPES)) + ".")
    width = _num(raw.get("width"), 0.0)
    height = _num(raw.get("height"), 0.0)
    if width < 0 or height < 0:
        raise ValueError("width/height must be >= 0.")
    if etype in ("rectangle", "ellipse", "diamond") and width == 0 and height == 0:
        width, height = 200.0, 120.0
    if etype in ("arrow", "line") and width == 0 and height == 0:
        width, height = 120.0, 0.0
    text = _str(raw.get("text", ""), "", _MAX_TEXT)
    if etype != "text":
        text = ""
    points = None
    if etype in ("arrow", "line"):
        pts = raw.get("points")
        if isinstance(pts, list) and pts:
            clean = []
            for p in pts[:500]:
                if (isinstance(p, (list, tuple)) and len(p) == 2):
                    clean.append([_num(p[0]), _num(p[1])])
            if len(clean) >= 2:
                points = clean
        if points is None:
            points = [[0.0, 0.0], [width, height]]
    el = {
        "id": _clean_id(raw.get("id")),
        "type": etype,
        "x": _num(raw.get("x")),
        "y": _num(raw.get("y")),
        "width": width,
        "height": height,
        "angle": max(-math.pi, min(math.pi, _num(raw.get("angle")))),
        "strokeColor": _str(raw.get("strokeColor"), "#1e1e1e", 32),
        "backgroundColor": _str(raw.get("backgroundColor"), "transparent", 32),
        "fillStyle": _str(raw.get("fillStyle"), "solid", 16),
        "strokeWidth": min(10.0, max(0.5, _num(raw.get("strokeWidth"), 2.0))),
        "strokeStyle": _str(raw.get("strokeStyle"), "solid", 16),
        "roughness": min(2.0, max(0.0, _num(raw.get("roughness"), 1.0))),
        "opacity": min(100.0, max(0.0, _num(raw.get("opacity"), 100.0))),
        "isDeleted": _bool(raw.get("isDeleted")),
        "locked": _bool(raw.get("locked")),
    }
    if el["fillStyle"] not in _FILL_STYLES:
        el["fillStyle"] = "solid"
    if el["strokeStyle"] not in _STROKE_STYLES:
        el["strokeStyle"] = "solid"
    if etype == "text":
        el["text"] = text
        el["fontSize"] = min(160.0, max(8.0, _num(raw.get("fontSize"), 20.0)))
        el["fontFamily"] = int(min(4, max(1, _num(raw.get("fontFamily"), 1))))
        el["textAlign"] = _str(raw.get("textAlign"), "left", 16)
        el["verticalAlign"] = _str(raw.get("verticalAlign"), "top", 16)
        if el["textAlign"] not in _ALIGNMENTS:
            el["textAlign"] = "left"
        if el["verticalAlign"] not in _VALIGNMENTS:
            el["verticalAlign"] = "top"
    if points is not None:
        el["points"] = points
        for head in ("startArrowhead", "endArrowhead"):
            ah = raw.get(head)
            el[head] = ah if ah in _ARROWHEADS else None
    groups = raw.get("groupIds")
    if isinstance(groups, list) and groups:
        el["groupIds"] = [_str(g, "", 64) for g in groups[:20] if g]
    cid = raw.get("containerId")
    el["containerId"] = cid.strip()[:64] if isinstance(cid, str) and cid.strip() else None
    return el


def make_label(shape, text, font_size=20.0):
    """Build a centered text element bound to a shape.

    Excalidraw renders text with a matching containerId inside the shape.
    Used when the AI puts \"text\" on a non-text element (shapes cannot
    carry text themselves - without this the label would be silently
    dropped and the AI told \"ok\").
    """
    try:
        fs = min(160.0, max(8.0, float(font_size)))
    except (TypeError, ValueError):
        fs = 20.0
    w = max(float(shape.get("width", 0) or 0), 10.0)
    h = max(float(shape.get("height", 0) or 0), 10.0)
    label_w = max(w - 20.0, 10.0)
    return {
        "id": uuid.uuid4().hex[:12],
        "type": "text",
        "x": float(shape.get("x", 0) or 0) + 10.0,
        "y": float(shape.get("y", 0) or 0) + max((h - 30.0) / 2.0, 0.0),
        "width": label_w,
        "height": 30.0,
        "angle": 0.0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2.0,
        "strokeStyle": "solid",
        "roughness": 1.0,
        "opacity": 100.0,
        "isDeleted": False,
        "locked": False,
        "text": str(text or "")[:20000],
        "fontSize": fs,
        "fontFamily": 1,
        "textAlign": "center",
        "verticalAlign": "middle",
        "containerId": shape.get("id"),
    }


def attach_labels(clean, raw):
    """Auto-create bound text labels for shapes carrying text.

    ``clean`` and ``raw`` are parallel lists (validated / original).
    Shapes cannot carry text themselves - without this the label would be
    silently lost while the tool reports success. Returns the new label
    elements (already validated dicts, ready to append).
    """
    labels = []
    for raw_el, el in zip(raw, clean, strict=True):
        if not isinstance(raw_el, dict):
            continue
        txt = raw_el.get("text")
        if txt and el.get("type") != "text":
            labels.append(make_label(
                el, txt, raw_el.get("fontSize", 20)))
    return labels


def empty_scene(title="Untitled board"):
    return {"version": SCENE_VERSION, "title": (title or "").strip() or "Untitled board",
            "elements": [], "appState": {"viewBackgroundColor": "#ffffff"},
            "ai_lock": False, "ai_hidden": False}


def validate_scene(scene):
    if not isinstance(scene, dict):
        raise ValueError("A scene must be an object with an 'elements' list.")
    elements = scene.get("elements", [])
    if not isinstance(elements, list):
        raise ValueError("'elements' must be a list.")
    if len(elements) > _MAX_ELEMENTS:
        raise ValueError(f"Too many elements (max {_MAX_ELEMENTS}).")
    return {
        "version": SCENE_VERSION,
        "title": _str(scene.get("title"), "Untitled board", 120),
        "elements": [validate_element(e) for e in elements],
        "appState": {"viewBackgroundColor": _str(
            (scene.get("appState") or {}).get("viewBackgroundColor"),
            "#ffffff", 32)},
        "ai_lock": _bool(scene.get("ai_lock")),
        "ai_hidden": _bool(scene.get("ai_hidden")),
    }


def _wants_auto(raw):
    """True when a raw element carries no explicit position at all."""
    return isinstance(raw, dict) and "x" not in raw and "y" not in raw


def auto_place(elements, auto_idx, existing=()):
    """Flow unpositioned elements into free space.

    ``auto_idx`` holds the indexes (into ``elements``) that had no
    explicit x/y in the raw input; only those move - everything else,
    including an explicit 0,0, is left untouched. New items arrange
    left-to-right below the ``existing`` content, wrapping every few
    items. Returns the count placed.
    """
    auto_idx = set(auto_idx)
    if not auto_idx:
        return 0
    bottom = 0.0
    for e in list(existing):
        try:
            bottom = max(bottom, float(e.get("y", 0))
                         + float(e.get("height", 0)))
        except (TypeError, ValueError):
            continue
    x, y, row_h, per_row = 40.0, bottom + 60.0, 0.0, 0
    placed = 0
    for i, e in enumerate(elements):
        if i not in auto_idx:
            continue
        w, h = e.get("width", 200.0), e.get("height", 100.0)
        if per_row >= 3 or x + w > 1100:
            x, y, row_h, per_row = 40.0, y + row_h + 60.0, 0.0, 0
        e["x"], e["y"] = x, y
        x += w + 40.0
        row_h = max(row_h, h)
        per_row += 1
        placed += 1
    return placed


def serialize(scene):
    return json.dumps(scene, ensure_ascii=False)


def parse(text):
    try:
        scene = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Not a whiteboard file: {exc}") from exc
    return validate_scene(scene)


def load(stored):
    return parse(file_service.read_text_content(stored))


def extract(stored_file):
    """Flatten a board to plain text for the search index."""
    try:
        scene = load(stored_file)
    except (ValueError, OSError):
        return ""
    parts = [scene.get("title", "")]
    for el in scene.get("elements", []):
        if el.get("type") == "text" and el.get("text"):
            parts.append(el["text"])
    return "\n".join(p for p in parts if p)


def get_board(file_id, user_id):
    return (StoredFile.query
            .filter_by(id=file_id, user_id=user_id, extension=EXTENSION)
            .first())


def list_boards(user_id):
    return (StoredFile.query
            .filter_by(user_id=user_id, extension=EXTENSION)
            .filter(StoredFile.deleted_at.is_(None))
            .order_by(StoredFile.updated_at.desc()).all())


def _guard_writable(stored):
    from app.services.file_service import is_readonly_file
    if is_readonly_file(stored):
        raise ValueError("Synced drives are read-only.")


def _guard_writable_drive(drive):
    if isinstance(drive, (list, tuple)):
        raise ValueError("A single target drive is required — pick one with "
                         "drive_id.")
    if drive.is_readonly:
        raise ValueError("Synced drives are read-only — whiteboards cannot "
                         "be created there.")


def create_board(user, drive, name, elements=None):
    """Create a whiteboard file in a writable drive. Returns StoredFile."""
    if isinstance(drive, (list, tuple)):
        drive = next((d for d in drive
                      if not getattr(d, "is_readonly", False)), None)
        if drive is None:
            raise ValueError("All drives in context are synced (read-only) — "
                             "pick a writable drive.")
    _guard_writable_drive(drive)
    scene = empty_scene(name)
    raw = list(elements or [])
    clean = [validate_element(e) for e in raw]
    # Elements without any coordinates flow into free space instead of
    # piling up at 0,0 (small models often omit geometry entirely).
    # Text on shapes becomes bound labels (shapes cannot carry text).
    auto_place(clean,
               {i for i, e in enumerate(raw) if _wants_auto(e)})
    scene["elements"] = clean + attach_labels(clean, raw)
    data = serialize(scene).encode("utf-8")
    storage = FileStorage(stream=BytesIO(data),
                          filename=f"{scene['title']}.{EXTENSION}",
                          content_type=MIME_TYPE)
    stored = file_service.save_upload(storage, user, drive=drive)
    stored.drive_id = drive.id
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    return stored


def save_board(stored, scene, note="", source="edit", skip_snapshot=False):
    """Persist a scene: atomic write + re-index. Snapshots history unless
    skipped (editor autosaves skip; explicit saves and AI edits keep one)."""
    _guard_writable(stored)
    scene = validate_scene(scene)
    if not skip_snapshot:
        file_service.snapshot_version(stored, source, note=note)
    data = serialize(scene)
    path = file_service.file_path(stored)
    with file_service.file_lock(stored.id):
        tmp_path = f"{path}.tmp-{uuid.uuid4().hex}"
        with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        os.replace(tmp_path, path)
    stored.size = len(data.encode("utf-8"))
    stored.updated_at = utcnow()
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    return scene


def scene_version(stored):
    """Opaque version string the editor polls for live AI updates."""
    ts = stored.updated_at
    return ts.isoformat() if ts else ""


def ai_error(stored, write=False):
    """Error message when the AI may not touch this board, else None."""
    try:
        scene = load(stored)
    except (ValueError, OSError):
        return None
    if scene.get("ai_hidden"):
        return ("This whiteboard is hidden from the AI by the user — do not "
                "read or mention its content. Tell the user they can unhide "
                "it from the board page.")
    if write and scene.get("ai_lock"):
        return ("This whiteboard is LOCKED against AI edits by the user. Do "
                "not generate or propose the content in chat — just tell "
                "the user the board is locked and ask them to unlock it "
                "from the board page, then repeat the request.")
    return None


def access_flags(stored):
    try:
        scene = load(stored)
    except (ValueError, OSError):
        return False, False
    return bool(scene.get("ai_lock")), bool(scene.get("ai_hidden"))


def ai_file_guard(_user, stored):
    if stored.extension != EXTENSION:
        return None
    return ai_error(stored)


def arrange(elements, mode, ids=None):
    """Align/distribute elements by bounding box. Returns a new list."""
    modes = {"align-left", "align-center-x", "align-right",
             "align-top", "align-center-y", "align-bottom",
             "distribute-x", "distribute-y"}
    if mode not in modes:
        raise ValueError(f"Unknown arrange mode {mode!r} — use one of: "
                         + ", ".join(sorted(modes)) + ".")
    target = [e for e in elements
              if ids is None or e.get("id") in set(ids or [])]
    if len(target) < (3 if mode.startswith("distribute") else 2):
        raise ValueError("Select at least "
                         + ("3" if mode.startswith("distribute") else "2")
                         + " elements.")
    boxes = {e["id"]: (e["x"], e["y"], e["x"] + e["width"],
                        e["y"] + e["height"]) for e in target}
    out = [dict(e) for e in elements]
    by_id = {e["id"]: e for e in out}
    if mode == "align-left":
        x0 = min(b[0] for b in boxes.values())
        for eid in boxes:
            by_id[eid]["x"] = x0
    elif mode == "align-right":
        x1 = max(b[2] for b in boxes.values())
        for eid in boxes:
            by_id[eid]["x"] = x1 - (boxes[eid][2] - boxes[eid][0])
    elif mode == "align-center-x":
        cx = sum(b[0] + b[2] for b in boxes.values()) / (2 * len(boxes))
        for eid in boxes:
            by_id[eid]["x"] = cx - (boxes[eid][2] - boxes[eid][0]) / 2
    elif mode == "align-top":
        y0 = min(b[1] for b in boxes.values())
        for eid in boxes:
            by_id[eid]["y"] = y0
    elif mode == "align-bottom":
        y1 = max(b[3] for b in boxes.values())
        for eid in boxes:
            by_id[eid]["y"] = y1 - (boxes[eid][3] - boxes[eid][1])
    elif mode == "align-center-y":
        cy = sum(b[1] + b[3] for b in boxes.values()) / (2 * len(boxes))
        for eid in boxes:
            by_id[eid]["y"] = cy - (boxes[eid][3] - boxes[eid][1]) / 2
    elif mode == "distribute-x":
        ordered = sorted(boxes, key=lambda i: boxes[i][0])
        lo, hi = boxes[ordered[0]][0], boxes[ordered[-1]][2]
        total_w = sum(boxes[i][2] - boxes[i][0] for i in ordered)
        gap = (hi - lo - total_w) / (len(ordered) - 1) if len(ordered) > 1 else 0
        gap = max(gap, 0)
        x = lo
        for eid in ordered:
            by_id[eid]["x"] = x
            x += (boxes[eid][2] - boxes[eid][0]) + gap
    else:  # distribute-y
        ordered = sorted(boxes, key=lambda i: boxes[i][1])
        lo, hi = boxes[ordered[0]][1], boxes[ordered[-1]][3]
        total_h = sum(boxes[i][3] - boxes[i][1] for i in ordered)
        gap = (hi - lo - total_h) / (len(ordered) - 1) if len(ordered) > 1 else 0
        gap = max(gap, 0)
        y = lo
        for eid in ordered:
            by_id[eid]["y"] = y
            y += (boxes[eid][3] - boxes[eid][1]) + gap
    return out
