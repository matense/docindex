"""AI tools contributed by the notebooks module (namespaced notebooks.*).

Mutations go through doc.save_document_ai: all edits an agent makes while
answering a single message share one snapshot of the pre-edit state, so a
whole AI answer is a single restorable block in the notebook's history.
"""

from app.extensions import db
from app.models import Drive, Folder
from app.services import hashtag_service
from app.services.agent_service import register_tool

from . import doc as doc_service


def _schema(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required)}}}


def _get_owned(user, file_id):
    stored = doc_service.get_notebook(file_id, user.id)
    if stored is None:
        return None, {"error": f"Notebook {file_id} not found."}
    try:
        doc_service._guard_writable(stored)
    except ValueError as exc:
        return None, {"error": str(exc)}
    return stored, None


def _notebook_summary(stored):
    tags = hashtag_service.get_tags(stored.index)
    ai_lock, _ai_hidden = doc_service.access_flags(stored)
    return {
        "id": stored.id, "name": stored.name,
        "drive": stored.drive.name if stored.drive else None,
        "folder": stored.folder.name if stored.folder else None,
        "cells": doc_service.cell_count(stored),
        "size": stored.size, "hashtags": tags,
        # The AI must see the lock up front so it asks the user to unlock
        # instead of generating content that cannot be saved.
        "ai_lock": ai_lock,
        "updated_at": stored.updated_at.isoformat() if stored.updated_at else None,
    }


def _tool_list(user, args, drive):
    notebooks = doc_service.list_notebooks(user.id)
    if drive is not None:
        notebooks = [n for n in notebooks if n.drive_id == drive.id]
    # AI-hidden notebooks are invisible to the assistant entirely.
    notebooks = [n for n in notebooks
                 if not doc_service.ai_error(n)]
    return {"total": len(notebooks),
            "notebooks": [_notebook_summary(n) for n in notebooks[:100]]}


def _tool_read(user, args, drive):
    stored = doc_service.get_notebook(args.get("file_id"), user.id)
    if stored is None:
        return {"error": f"Notebook {args.get('file_id')} not found."}
    guarded = doc_service.ai_error(stored)
    if guarded:
        return {"error": guarded}
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    cells = document["cells"]
    start = max(0, int(args.get("start", 0)))
    count = min(100, int(args.get("count", 50)))
    result = {"id": stored.id, "title": document["title"],
              "total_cells": len(cells),
              "ai_lock": bool(document.get("ai_lock")),
              "cells": [{"index": start + i, **c}
                        for i, c in enumerate(cells[start:start + count])]}
    if result["ai_lock"]:
        result["note"] = ("This notebook is LOCKED against AI edits by the "
                          "user — do not generate or propose changes; ask "
                          "them to unlock it with the lock button in the "
                          "notebook toolbar first.")
    return result


def _tool_create(user, args, drive):
    target_drive = None
    folder = None
    if args.get("folder_id"):
        folder = db.session.get(Folder, int(args["folder_id"]))
        if folder is None or folder.user_id != user.id:
            return {"error": "Folder not found."}
        target_drive = folder.drive
    elif args.get("drive_id"):
        target_drive = db.session.get(Drive, int(args["drive_id"]))
        if target_drive is None or target_drive.user_id != user.id:
            return {"error": "Drive not found."}
    elif drive is not None:
        target_drive = drive
    else:
        target_drive = (Drive.query
                        .filter_by(user_id=user.id, source_path=None)
                        .order_by(Drive.id).first())
    if target_drive is None:
        return {"error": "No writable drive found — create one first."}
    try:
        cells = [doc_service.new_cell(c.get("type", "markdown"),
                                      c.get("content", ""), c.get("meta"))
                 for c in (args.get("cells") or [])]
        stored = doc_service.create_notebook(
            user, target_drive, args.get("name", ""), folder=folder,
            cells=cells or None)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"created": _notebook_summary(stored)}


def _save_and_report(user, file_id, mutate):
    stored, error = _get_owned(user, file_id)
    if error:
        return error
    # AI-hidden notebooks refuse all access; AI-locked ones refuse edits.
    guarded = doc_service.ai_error(stored, write=True)
    if guarded:
        return {"error": guarded}
    try:
        document = doc_service.load(stored)
        cell = mutate(document)
        doc_service.save_document_ai(stored, document)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, "file_id": stored.id, "cell": cell,
            "total_cells": len(document["cells"])}


def _tool_add_cell(user, args, drive):
    return _save_and_report(
        user, args.get("file_id"),
        lambda document: doc_service.add_cell(
            document, args.get("cell_type", "markdown"),
            args.get("content", ""), args.get("meta"),
            index=args.get("index")))


def _tool_update_cell(user, args, drive):
    return _save_and_report(
        user, args.get("file_id"),
        lambda document: doc_service.update_cell(
            document, args.get("cell_id", ""),
            content=args.get("content"), meta=args.get("meta")))


def _tool_delete_cell(user, args, drive):
    return _save_and_report(
        user, args.get("file_id"),
        lambda document: doc_service.delete_cell(
            document, args.get("cell_id", "")))


_FILE_ID = {"type": "integer", "description": "Notebook file id."}

register_tool(
    "list",
    _schema("notebooks.list",
            "List the user's notebooks (Jupyter-style .pdocnb cell documents) "
            "with drive, folder, cell count and hashtags. Each entry has an "
            "'ai_lock' flag: when true the notebook is locked against AI "
            "edits — do not generate or propose changes; ask the user to "
            "unlock it (lock button in the notebook toolbar) first.", {}),
    _tool_list, label="Listed notebooks", module="notebooks")

register_tool(
    "read",
    _schema("notebooks.read",
            "Read a notebook's cells (markdown/code/todo/table), paginated by "
            "cell index. The response includes 'ai_lock': when true the "
            "notebook is locked against AI edits — do not generate or "
            "propose changes; ask the user to unlock it (lock button in the "
            "notebook toolbar) first.",
            {"file_id": _FILE_ID,
             "start": {"type": "integer", "description": "First cell index."},
             "count": {"type": "integer",
                       "description": "Cells to return (max 100)."}},
            required=["file_id"]),
    _tool_read, label="Read notebook", module="notebooks")

register_tool(
    "create",
    _schema("notebooks.create",
            "Create a notebook in a writable (non-synced) drive, optionally "
            "with initial cells.",
            {"name": {"type": "string", "description": "Notebook name."},
             "drive_id": {"type": "integer",
                          "description": "Target drive (default: current)."},
             "folder_id": {"type": "integer",
                           "description": "Target folder inside its drive."},
             "cells": {"type": "array", "items": {"type": "object"},
                       "description": "Initial cells: {type, content, meta}."}},
            required=["name"]),
    _tool_create, label="Created notebook", module="notebooks")

register_tool(
    "add_cell",
    _schema("notebooks.add_cell",
            "Append or insert a cell (markdown/code/todo/table) in a "
            "notebook. All edits made while answering one message share a "
            "single restorable history version.",
            {"file_id": _FILE_ID,
             "cell_type": {"type": "string",
                           "enum": list(doc_service.CELL_TYPES)},
             "content": {"type": "string",
                         "description": "Markdown/code source."},
             "meta": {"type": "object",
                      "description": "todo: {items:[{text,done}]}; table: "
                                     "{headers:[],rows:[[]]}; code: "
                                     "{language}."},
             "index": {"type": "integer",
                       "description": "Insert position (default: append)."}},
            required=["file_id", "cell_type"]),
    _tool_add_cell, label="Added cell", module="notebooks")

register_tool(
    "update_cell",
    _schema("notebooks.update_cell",
            "Update a cell's content and/or meta by cell id. All edits made "
            "while answering one message share a single restorable history "
            "version.",
            {"file_id": _FILE_ID,
             "cell_id": {"type": "string"},
             "content": {"type": "string"},
             "meta": {"type": "object"}},
            required=["file_id", "cell_id"]),
    _tool_update_cell, label="Updated cell", module="notebooks")

register_tool(
    "delete_cell",
    _schema("notebooks.delete_cell",
            "Delete a cell by id. All edits made while answering one "
            "message share a single restorable history version.",
            {"file_id": _FILE_ID, "cell_id": {"type": "string"}},
            required=["file_id", "cell_id"]),
    _tool_delete_cell, label="Deleted cell", module="notebooks")
