"""AI tools contributed by the dashboards module (namespaced dashboards.*).

Mutations go through doc.save_document_ai: all edits an agent makes while
answering a single message share one snapshot of the pre-edit state, so a
whole AI answer is a single restorable block in the dashboard's history.
Card runs write results through without snapshotting (like the run routes),
so refreshing never explodes version storage.
"""

import importlib
import uuid
from io import BytesIO

from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import Drive, Folder, StoredFile, utcnow
from app.services import search_service
from app.services.agent_service import register_tool

from . import doc as doc_service
from . import engine as engine_service


def _schema(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required)}}}


def _get_owned(user, file_id):
    try:
        fid = int(file_id)
    except (TypeError, ValueError):
        return None, {"error": f"Dashboard {file_id!r} not found."}
    stored = doc_service.get_dashboard(fid, user.id)
    if stored is None:
        return None, {"error": f"Dashboard {file_id} not found."}
    try:
        doc_service._guard_writable(stored)
    except ValueError as exc:
        return None, {"error": str(exc)}
    return stored, None


def _summary(stored):
    ai_lock, _ai_hidden = doc_service.access_flags(stored)
    try:
        document = doc_service.load(stored)
        cards = len(document.get("cards", []))
    except (ValueError, OSError):
        cards = 0
    return {
        "id": stored.id, "name": stored.name,
        "drive": stored.drive.name if stored.drive else None,
        "folder": stored.folder.name if stored.folder else None,
        "cards": cards,
        # The AI must see the lock up front so it asks the user to unlock
        # instead of generating content that cannot be saved.
        "ai_lock": ai_lock,
        "updated_at": (stored.updated_at.isoformat()
                       if stored.updated_at else None),
    }


def _resolve_target(user, args, drive):
    """Writable (drive, folder) for creation, mirroring notebooks.create."""
    folder = None
    if args.get("folder_id"):
        try:
            folder_id = int(args["folder_id"])
        except (TypeError, ValueError):
            return None, None, "Invalid folder id."
        folder = db.session.get(Folder, folder_id)
        if folder is None or folder.user_id != user.id:
            return None, None, "Folder not found."
        return folder.drive, folder, None
    target_drive = None
    if args.get("drive_id"):
        try:
            drive_id = int(args["drive_id"])
        except (TypeError, ValueError):
            return None, None, "Invalid drive id."
        target_drive = db.session.get(Drive, drive_id)
        if target_drive is None or target_drive.user_id != user.id:
            return None, None, "Drive not found."
    elif drive is not None:
        # The chat context can be a list of drives (multi-drive $ mentions):
        # dashboards need exactly one writable target, so take the first
        # non-synced drive in scope.
        candidates = list(drive) if isinstance(drive, (list, tuple)) else [drive]
        target_drive = next((d for d in candidates if not d.is_readonly), None)
        if target_drive is None and candidates:
            return None, None, ("All drives in context are synced "
                                "(read-only) — pick a writable drive with "
                                "drive_id.")
    else:
        target_drive = (Drive.query
                        .filter_by(user_id=user.id, source_path=None)
                        .order_by(Drive.id).first())
    if target_drive is None:
        return None, None, "No writable drive found — create one first."
    if target_drive.is_readonly:
        return None, None, ("Synced drives are read-only — dashboards cannot "
                            "be created there.")
    return target_drive, folder, None


def _automation_models():
    """The loaded automation models, whatever name the loader used."""
    for name in ("docindex_module_automation", "modules.automation"):
        try:
            package = importlib.import_module(name)
        except ImportError:
            continue
        return importlib.import_module(f"{package.__name__}.models")
    raise RuntimeError("Automation module is not available.")


def _check_workflow(user, ref):
    """Validate a card workflow reference at author time.

    Returns an error string, or None when the reference is usable. The id is
    normalized to int in place so runs never trip on a string id."""
    if ref is None:
        return None
    if not isinstance(ref, dict):
        return "Card workflow must be an object like " \
               "{workflow_id: 12, params: {...}}."
    try:
        wid = int(ref.get("workflow_id"))
    except (TypeError, ValueError):
        return "Card workflow needs an integer workflow_id — " \
               "re-open the Builder and pick a workflow."
    params = ref.get("params", {})
    if params is not None and not isinstance(params, dict):
        return "Card workflow params must be an object."
    try:
        models = _automation_models()
    except RuntimeError:
        return "The Automation module is not available — workflow cards " \
               "need it enabled."
    workflow = (models.AutomationWorkflow.query
                .filter_by(id=wid, user_id=user.id).first())
    if workflow is None:
        return f"Workflow #{wid} not found or not yours — pick another " \
               f"workflow in the Builder."
    ref["workflow_id"] = wid
    return None


def _card_from_args(args):
    """Build a card dict from tool args; raises ValueError on bad shape.

    Accepts either flat fields (type/title/prompt/tools/script_enabled/
    script/w/h/x/y/attachments/workflow) or the same fields nested under a
    ``card`` object; flat fields win."""
    data = args.get("card") if isinstance(args.get("card"), dict) else {}
    flat = dict(data)
    for key in ("type", "title", "prompt", "tools", "script_enabled",
                "script", "w", "h", "x", "y",
                "attachments", "workflow"):
        if key in args and args[key] is not None:
            flat[key] = args[key]
    card = doc_service.new_card(flat.pop("type", "kpi"))
    if "title" in flat:
        card["title"] = flat["title"]
    if "prompt" in flat:
        card["prompt"] = flat["prompt"]
    if "tools" in flat:
        card["tools"] = flat["tools"]
    if "script_enabled" in flat:
        card["script_enabled"] = bool(flat["script_enabled"])
    if "script" in flat:
        script = flat["script"]
        card["script"] = str(script) if script is not None else ""
    for key in ("w", "h", "x", "y"):
        if key in flat:
            card[key] = flat[key]
    if "attachments" in flat:
        refs = flat["attachments"]
        if not isinstance(refs, list):
            raise ValueError("Card attachments must be a list of "
                             "attachment ids.")
        card["inputs"]["attachments"] = refs
    if "workflow" in flat:
        workflow = flat["workflow"]
        if workflow is not None and not isinstance(workflow, dict):
            raise ValueError("Card workflow must be an object like "
                             "{workflow_id: 12, params: {...}}.")
        if workflow is None:
            card["inputs"].pop("workflow", None)
        else:
            card["inputs"]["workflow"] = workflow
    return card


def _check_card_workflows(user, card):
    workflow = (card.get("inputs") or {}).get("workflow")
    if workflow is not None:
        error = _check_workflow(user, workflow)
        if error:
            raise ValueError(error)


def _find_card(document, card_id):
    card = next((c for c in document.get("cards", [])
                 if c.get("id") == card_id), None)
    if card is None:
        raise ValueError(f"Unknown card '{card_id}'.")
    return card


def _save_and_report(user, file_id, mutate):
    stored, error = _get_owned(user, file_id)
    if error:
        return error
    # AI-hidden dashboards refuse all access; AI-locked ones refuse edits.
    guarded = doc_service.ai_error(stored, write=True)
    if guarded:
        return {"error": guarded}
    try:
        document = doc_service.load(stored)
        outcome = mutate(user, document)
        doc_service.save_document_ai(stored, document)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, "file_id": stored.id, **outcome}


def _tool_list(user, args, drive):
    dashboards = doc_service.list_dashboards(user.id)
    drive_ids = search_service.drive_id_list(drive)
    if drive_ids is not None:
        dashboards = [d for d in dashboards if d.drive_id in drive_ids]
    # AI-hidden dashboards are invisible to the assistant entirely.
    dashboards = [d for d in dashboards
                  if not doc_service.ai_error(d)]
    return {"total": len(dashboards),
            "dashboards": [_summary(d) for d in dashboards[:100]]}


def _tool_read(user, args, drive):
    stored, error = _get_owned(user, args.get("file_id"))
    if error:
        return error
    guarded = doc_service.ai_error(stored)
    if guarded:
        return {"error": guarded}
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    result = {"id": stored.id, "title": document.get("title", ""),
              "concurrency": document.get(
                  "concurrency", doc_service.DEFAULT_CONCURRENCY),
              "total_cards": len(document.get("cards", [])),
              "cards": document.get("cards", []),
              "attachments": document.get("attachments", []),
              "createdFileIds": document.get("createdFileIds", []),
              "ai_lock": bool(document.get("ai_lock"))}
    if result["ai_lock"]:
        result["note"] = ("This dashboard is LOCKED against AI edits by the "
                          "user — do not generate or propose changes; ask "
                          "them to unlock it with the lock button in the "
                          "dashboard toolbar first.")
    return result


def _tool_create(user, args, drive):
    target_drive, folder, error = _resolve_target(user, args, drive)
    if error:
        return {"error": error}
    raw_cards = args.get("cards") or []
    if not isinstance(raw_cards, list):
        return {"error": "Cards must be a list like "
                         "[{type: 'kpi', title: '...', prompt: '...'}]."}
    try:
        cards = []
        for c in raw_cards:
            if isinstance(c, dict):
                cards.append(_card_from_args(c))
            elif isinstance(c, str):
                cards.append(_card_from_args({"title": c}))
            else:
                raise ValueError(
                    "Each card must be an object like {type: 'kpi', "
                    "title: '...', prompt: '...'} or a plain string.")
        for card in cards:
            _check_card_workflows(user, card)
        # Validate before creating: attachment refs must already exist on a
        # dashboard, so cards with refs are rejected here without leaving an
        # empty dashboard behind (attach first, then wire via update_card).
        preview = doc_service.empty_dashboard(args.get("name", ""))
        preview["cards"].extend(cards)
        doc_service.validate(preview)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        stored = doc_service.create_dashboard(
            user, target_drive, args.get("name", ""), folder=folder)
        if cards:
            document = doc_service.load(stored)
            document["cards"].extend(preview["cards"])
            doc_service.save_document_ai(stored, document)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"created": _summary(stored)}


def _tool_update(user, args, drive):
    def mutate(_user, document):
        if "title" in args or "name" in args:
            document["title"] = args.get("title", args.get("name"))
        if "concurrency" in args:
            document["concurrency"] = args["concurrency"]
        return {"title": document["title"],
                "concurrency": document.get(
                    "concurrency", doc_service.DEFAULT_CONCURRENCY)}
    return _save_and_report(user, args.get("file_id"), mutate)


def _tool_add_card(user, args, drive):
    def mutate(actor, document):
        card = _card_from_args(args)
        _check_card_workflows(actor, card)
        document["cards"].append(card)
        # Validating the whole document enforces the grid snap and, most
        # importantly, that the card only references attachments already on
        # this dashboard — free-floating file ids are rejected here.
        doc_service.validate(document)
        return {"card": card, "total_cards": len(document["cards"])}
    return _save_and_report(user, args.get("file_id"), mutate)


def _tool_update_card(user, args, drive):
    def mutate(actor, document):
        card = _find_card(document, args.get("card_id"))
        data = args.get("card") if isinstance(args.get("card"), dict) else {}
        if "type" in args or "type" in data:
            new_type = args.get("type", data.get("type"))
            if new_type not in doc_service.CARD_TYPES:
                raise ValueError(f"Unknown card type '{new_type}'.")
            card["type"] = new_type
        for key in ("title", "prompt", "script_enabled"):
            if key in args or key in data:
                card[key] = args.get(key, data.get(key))
        if "script" in args or "script" in data:
            script = args.get("script", data.get("script"))
            card["script"] = str(script) if script is not None else ""
        if "tools" in args or "tools" in data:
            card["tools"] = args.get("tools", data.get("tools"))
        for key in ("w", "h", "x", "y"):
            if key in args or key in data:
                value = args.get(key, data.get(key))
                if value is not None:
                    card[key] = value
        if "attachments" in args or "attachments" in data:
            refs = args.get("attachments", data.get("attachments"))
            if refs is not None and not isinstance(refs, list):
                raise ValueError("Card attachments must be a list of "
                                 "attachment ids.")
            card["inputs"]["attachments"] = refs if refs is not None else []
        if "workflow" in args or "workflow" in data:
            workflow = args.get("workflow", data.get("workflow"))
            if workflow is None:
                card["inputs"].pop("workflow", None)
            elif not isinstance(workflow, dict):
                raise ValueError("Card workflow must be an object like "
                                 "{workflow_id: 12, params: {...}}.")
            else:
                card["inputs"]["workflow"] = workflow
        if ("workflow" in args or "workflow" in data
                or "attachments" in args or "attachments" in data):
            _check_card_workflows(actor, card)
        doc_service.validate(document)
        return {"card": card, "total_cards": len(document["cards"])}
    return _save_and_report(user, args.get("file_id"), mutate)


def _tool_delete_card(user, args, drive):
    def mutate(_user, document):
        card = _find_card(document, args.get("card_id"))
        document["cards"] = [c for c in document["cards"]
                             if c.get("id") != card["id"]]
        doc_service.validate(document)
        return {"deleted": card["id"],
                "total_cards": len(document["cards"])}
    return _save_and_report(user, args.get("file_id"), mutate)


def _tool_attach_file(user, args, drive):
    stored, error = _get_owned(user, args.get("file_id"))
    if error:
        return error
    guarded = doc_service.ai_error(stored, write=True)
    if guarded:
        return {"error": guarded}
    use_link = args.get("target_file_id") is not None
    use_upload = args.get("name") or args.get("content") is not None
    target_id = None
    if use_link:
        try:
            target_id = int(args["target_file_id"])
        except (TypeError, ValueError):
            return {"error": "Invalid target file id."}
        target = (StoredFile.query
                  .filter_by(id=target_id, user_id=user.id)
                  .filter(StoredFile.deleted_at.is_(None))
                  .first())
        if target is None:
            return {"error": f"File {target_id} not found or not owned by "
                             "you — attachments must reference your own "
                             "files."}
    elif not use_upload:
        return {"error": "Pass target_file_id to link an existing file, "
                         "or name + content to upload new text."}
    try:
        # Plant the AI history block first: the attach helpers save through
        # doc.save_document directly, so without this the block would be
        # labelled a plain edit instead of part of this AI answer.
        doc_service.save_document_ai(stored, doc_service.load(stored))
        if use_link:
            attached = doc_service.attach_link(stored, target_id)
        else:
            name = (args.get("name") or "attachment.txt").strip()
            upload = FileStorage(
                stream=BytesIO((args.get("content") or "")
                               .encode("utf-8")),
                filename=name, content_type="text/plain")
            attached = doc_service.attach_upload(stored, upload)
        document = doc_service.load(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, "file_id": stored.id, "attached": attached,
            "total_attachments": len(document.get("attachments", []))}


def _tool_detach_file(user, args, drive):
    stored, error = _get_owned(user, args.get("file_id"))
    if error:
        return error
    guarded = doc_service.ai_error(stored, write=True)
    if guarded:
        return {"error": guarded}
    att_id = args.get("attachment_id", args.get("attachment"))
    if not att_id:
        return {"error": "Pass the attachment id to detach."}
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    if not any(a.get("id") == att_id
               for a in document.get("attachments", [])):
        return {"error": f"Attachment '{att_id}' not found."}
    try:
        doc_service.save_document_ai(stored, document)
        result = doc_service.detach_attachment(stored, att_id)
        document = doc_service.load(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, "file_id": stored.id, **result,
            "total_attachments": len(document.get("attachments", []))}


def _settle_run(stored, card_id, run_id, user_id):
    """Execute one card synchronously and persist its outcome.

    Mirrors the POST /run route: MissingInputError and crashes become the
    card's visible error state, artifacts join the registry, and the result
    writes through without snapshotting a version."""
    with engine_service.dashboard_lock(stored.id):
        document = doc_service.load(stored)
        cap = document.get("concurrency",
                           doc_service.DEFAULT_CONCURRENCY)
        running = [c.get("id") for c in document.get("cards", [])
                   if c.get("status") == "running"]
        if card_id in running:
            return {"error": "That card is already running — wait for it "
                             "to finish, then try again.",
                    "already_running": [card_id]}
        if len(running) >= cap:
            return {"error": "The dashboard is already running "
                             f"{len(running)} card(s) — wait for one to "
                             "finish, then try again.",
                    "already_running": running}
        card = next((c for c in document["cards"]
                     if c.get("id") == card_id), None)
        if card is None:
            return {"error": f"Unknown card '{card_id}'."}
        card["status"] = "running"
        card["runId"] = run_id
        doc_service.save_document(stored, document)
        doc_service.run_started(stored.id, run_id)
        try:
            fresh = doc_service.load(stored)
            target = next(c for c in fresh["cards"]
                          if c.get("id") == card_id)
            result, artifact_ids, trace = engine_service.run_card(
                stored, target, user_id)
            status = "done"
        except engine_service.MissingInputError as exc:
            result, artifact_ids, trace, status = (
                {"error": str(exc)}, [], [], "error")
        except Exception as exc:  # noqa: BLE001 - errors surface on card
            db.session.rollback()
            result, artifact_ids, trace, status = (
                {"error": (str(exc) or "The run failed.")[:500]},
                [], [], "error")
        finally:
            elapsed = doc_service.run_elapsed(stored.id, run_id)
            doc_service.run_finished(stored.id, run_id)
        settled = doc_service.load(stored)
        target = next(c for c in settled["cards"]
                      if c.get("id") == card_id)
        target["status"] = status
        target["result"] = result
        target["trace"] = trace
        target["ranAt"] = utcnow().isoformat()
        if elapsed is not None:
            target["ranSecs"] = elapsed
        registry = settled.setdefault("createdFileIds", [])
        for new_id in artifact_ids:
            if new_id not in registry:
                registry.append(new_id)
        doc_service.save_document(stored, settled)
    if status == "error":
        message = result.get("error") if isinstance(result, dict) \
            else str(result)
        return {"error": message, "status": status, "result": result,
                "file_id": stored.id, "card_id": card_id, "run_id": run_id}
    return {"ok": True, "file_id": stored.id, "card_id": card_id,
            "run_id": run_id, "status": status, "result": result}


def _tool_run_card(user, args, drive):
    stored, error = _get_owned(user, args.get("file_id"))
    if error:
        return error
    # Runs mutate the doc (result/status/trace/artifacts), so locked and
    # hidden dashboards refuse them like any other AI edit.
    guarded = doc_service.ai_error(stored, write=True)
    if guarded:
        return {"error": guarded}
    if not args.get("card_id"):
        return {"error": "Pass the card id to run."}
    try:
        doc_service.load(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    run_id = f"{stored.id}:{uuid.uuid4().hex[:12]}"
    return _settle_run(stored, args["card_id"], run_id, user.id)


def _tool_run_all(user, args, drive):
    stored, error = _get_owned(user, args.get("file_id"))
    if error:
        return error
    guarded = doc_service.ai_error(stored, write=True)
    if guarded:
        return {"error": guarded}
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    targets = [c.get("id") for c in document.get("cards", [])]
    running = [c.get("id") for c in document.get("cards", [])
               if c.get("status") == "running"]
    if running:
        return {"error": "Another run is already in progress on this "
                         "dashboard — wait for it to finish, then try "
                         "again.",
                "already_running": running}
    runs = []
    for target in targets:
        run_id = f"{stored.id}:{uuid.uuid4().hex[:12]}"
        outcome = _settle_run(stored, target, run_id, user.id)
        if "error" in outcome and "status" not in outcome:
            return outcome
        runs.append({"card_id": target, "run_id": run_id,
                     "status": outcome.get("status")})
    return {"ok": True, "file_id": stored.id, "runs": runs}


def _recommend(document, owner_id):
    """Heuristic review returning concrete, applicable card edits."""
    recs = []
    cards = document.get("cards", [])
    attachments = {a.get("id"): a for a in
                   document.get("attachments", [])}
    if not cards:
        starters = [
            ("kpi", "Open incidents",
             "Count the open incidents in the attached files."),
            ("table", "Latest incidents",
             "List the 10 most recent incidents with their severity."),
            ("chart", "Incidents by severity",
             "Break down incident counts by severity for a bar chart."),
            ("markdown", "Executive summary",
             "Write a short executive summary of the current state."),
        ]
        for ctype, title, prompt in starters:
            try:
                proposed = doc_service.new_card(
                    ctype, title=title, prompt=prompt)
            except ValueError:
                continue
            recs.append({
                "kind": "add_card",
                "message": f"This dashboard is empty — start with a "
                           f"{ctype} card ('{title}').",
                "proposed_card": proposed,
            })
        return recs
    for card in cards:
        cid = card.get("id")
        ctype = card.get("type")
        workflow = (card.get("inputs") or {}).get("workflow")
        prompt = (card.get("prompt") or "").strip()
        if not prompt and not workflow:
            title = card.get("title") or f"the {ctype} card"
            recs.append({
                "kind": "update_card",
                "card_id": cid,
                "message": f"Card '{title}' has no prompt and no workflow "
                           f"— it has nothing to run.",
                "suggested_edit": {
                    "prompt": f"Summarize the attached files for "
                              f"'{title}'."},
            })
        if (ctype == "workflow" or workflow) and not workflow:
            recs.append({
                "kind": "update_card",
                "card_id": cid,
                "message": f"Card '{card.get('title') or ctype}' is a "
                           f"workflow card without a linked workflow — "
                           f"pick one in the Builder.",
                "suggested_edit": {"workflow": {"workflow_id": None,
                                               "params": {}}},
            })
        for ref in (card.get("inputs") or {}).get("attachments", []):
            entry = attachments.get(ref)
            refs = [r for r in
                    (card.get("inputs") or {}).get("attachments", [])
                    if r != ref]
            if entry is None:
                recs.append({
                    "kind": "update_card",
                    "card_id": cid,
                    "message": f"Card references attachment '{ref}' which "
                               f"is no longer on this dashboard — remove "
                               f"the reference from the card.",
                    "suggested_edit": {"attachments": refs},
                })
                continue
            target = db.session.get(StoredFile, entry.get("fileId"))
            if (target is None or target.user_id != owner_id
                    or target.deleted_at is not None):
                recs.append({
                    "kind": "update_card",
                    "card_id": cid,
                    "message": f"Missing input: '{entry.get('name') or ref}' "
                               f"was trashed, moved or deleted — remove the "
                               f"reference from the card (detach is blocked "
                               f"while cards link it).",
                    "suggested_edit": {"attachments": refs},
                })
        result = card.get("result") or {}
        if card.get("status") == "error":
            detail = result.get("error") if isinstance(result, dict) \
                else str(result)
            recs.append({
                "kind": "run_card",
                "card_id": cid,
                "message": f"Card '{card.get('title') or ctype}' failed: "
                           f"{detail} Fix the input, then run it again.",
                "suggested_edit": {"run_card_id": cid},
            })
        elif isinstance(result, dict) and result.get("truncated"):
            recs.append({
                "kind": "update_card",
                "card_id": cid,
                "message": f"Card '{card.get('title') or ctype}' ran with "
                           f"truncated inputs (tools off) — enable tools so "
                           f"the agent reads the full files.",
                "suggested_edit": {"tools": True},
            })
        elif card.get("status") == "idle" and not card.get("ranAt"):
            recs.append({
                "kind": "run_card",
                "card_id": cid,
                "message": f"Card '{card.get('title') or ctype}' never ran — "
                           f"run it to snapshot its first result.",
                "suggested_edit": {"run_card_id": cid},
            })
    return recs


def _tool_recommend(user, args, drive):
    stored, error = _get_owned(user, args.get("file_id"))
    if error:
        return error
    guarded = doc_service.ai_error(stored)
    if guarded:
        return {"error": guarded}
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"ok": True, "file_id": stored.id,
            "recommendations": _recommend(document, stored.user_id)}


_FILE_ID = {"type": "integer", "description": "Dashboard file id."}

_CARD_FIELDS = {
    "type": {"type": "string",
             "enum": list(doc_service.CARD_TYPES),
             "description": "Card type: kpi, table, list, chart, markdown "
                            "or workflow."},
    "title": {"type": "string", "description": "Card title."},
    "prompt": {"type": "string",
               "description": "Natural-language instruction executed on "
                              "each run."},
    "tools": {"type": "boolean",
              "description": "Run the full agent with the tool loop (true, "
                             "default) or a single cheaper LLM call over "
                             "pre-injected context (false, truncated inputs "
                             "are flagged)."},
    "script_enabled": {"type": "boolean",
                       "description": "When true the card runs the script "
                                      "via pyrun instead of the AI agent; "
                                      "the prompt is ignored."},
    "script": {"type": "string",
               "description": "Python script source for a script card; it "
                              "runs via pyrun when script_enabled is true "
                              "and the prompt is ignored. Attachments seed "
                              "scratch tables t0..tN in card order — probe "
                              "them with db.tables(), never try/except over "
                              "guessed t-names (sandbox errors abort). "
                              "Dashboard variables {{ name }} substitute "
                              "into the script source before each run, the "
                              "same as into prompts."},
    "w": {"type": "integer",
          "description": "Width in grid columns (1-12, snapped)."},
    "h": {"type": "integer", "description": "Height in grid rows."},
    "x": {"type": "integer", "description": "Optional grid column."},
    "y": {"type": "integer", "description": "Optional grid row."},
    "attachments": {"type": "array", "items": {"type": "string"},
                    "description": "Attachment ids from this dashboard "
                                   "only (attach files first with "
                                   "dashboards.attach_file) — never raw "
                                   "file ids."},
    "workflow": {"type": "object",
                 "description": "Optional {workflow_id, params} automation "
                                "reference for workflow cards."},
}

register_tool(
    "list",
    _schema("dashboards.list",
            "List the user's dashboards (prompt-driven card pages) with "
            "drive, folder and card count. Each entry has an 'ai_lock' "
            "flag: when true the dashboard is locked against AI edits — "
            "do not generate or propose changes; ask the user to unlock "
            "it first.",
            {}),
    _tool_list, label="Listed dashboards", module="dashboards")

register_tool(
    "read",
    _schema("dashboards.read",
            "Read a dashboard: title, cards (type/title/prompt/inputs/"
            "result/status/ranAt), attachments and run-created file ids. "
            "The response includes 'ai_lock': when true the dashboard is "
            "locked against AI edits — do not generate or propose "
            "changes; ask the user to unlock it first.",
            {"file_id": _FILE_ID},
            required=["file_id"]),
    _tool_read, label="Read dashboard", module="dashboards")

register_tool(
    "create",
    _schema("dashboards.create",
            "Create a dashboard in a writable (non-synced) drive, "
            "optionally with initial cards. Cards reference workflow ids "
            "and prompts only — attach files afterwards with "
            "dashboards.attach_file, then wire their attachment ids with "
            "dashboards.update_card. All edits made while answering one "
            "message share a single restorable history version.",
            {"name": {"type": "string",
                      "description": "Dashboard name."},
             "drive_id": {"type": "integer",
                          "description": "Target drive (default: current)."},
             "folder_id": {"type": "integer",
                           "description": "Target folder inside its drive."},
             "cards": {"type": "array", "items": {"type": "object"},
                       "description": "Initial cards: {type, title, prompt, "
                                      "tools, script_enabled, script, w, h, "
                                      "x, y, workflow}. When script_enabled "
                                      "is true the card runs the script via "
                                      "pyrun and the prompt is ignored. "
                                      "Attachment refs are not accepted "
                                      "here — attach first, then wire."}},
            required=["name"]),
    _tool_create, label="Created dashboard", module="dashboards")

register_tool(
    "update",
    _schema("dashboards.update",
            "Rename a dashboard and/or change its run-queue concurrency "
            "(1-8, default 1). All edits made while answering one message "
            "share a single restorable history version.",
            {"file_id": _FILE_ID,
             "title": {"type": "string",
                       "description": "New dashboard title."},
             "concurrency": {"type": "integer",
                             "description": "How many cards may run at "
                                            "once (1-8)."}},
            required=["file_id"]),
    _tool_update, label="Updated dashboard", module="dashboards")

register_tool(
    "add_card",
    _schema("dashboards.add_card",
            "Add a card to a dashboard. Card inputs.attachments must be "
            "attachment ids already on THIS dashboard (attach with "
            "dashboards.attach_file first) — raw file ids are rejected. "
            "All edits made while answering one message share a single "
            "restorable history version.",
            {"file_id": _FILE_ID, **_CARD_FIELDS},
            required=["file_id"]),
    _tool_add_card, label="Added card", module="dashboards")

register_tool(
    "update_card",
    _schema("dashboards.update_card",
            "Update a card's title, prompt, tools toggle, size, position, "
            "attachment refs (dashboard attachment ids only) and/or "
            "workflow reference by card id. Omit workflow (or pass null) "
            "to unlink it. All edits made while answering one message "
            "share a single restorable history version.",
            {"file_id": _FILE_ID,
             "card_id": {"type": "string",
                         "description": "Card id to update."},
             **_CARD_FIELDS},
            required=["file_id", "card_id"]),
    _tool_update_card, label="Updated card", module="dashboards")

register_tool(
    "delete_card",
    _schema("dashboards.delete_card",
            "Delete a card by id. All edits made while answering one "
            "message share a single restorable history version.",
            {"file_id": _FILE_ID,
             "card_id": {"type": "string"}},
            required=["file_id", "card_id"]),
    _tool_delete_card, label="Deleted card", module="dashboards")

register_tool(
    "attach_file",
    _schema("dashboards.attach_file",
            "Attach a file to a dashboard: link an existing owned file "
            "with target_file_id, or upload new text with name + content "
            "(stored in the dashboard's folder, dashboard-owned). Cards "
            "reference the returned attachment id.",
            {"file_id": _FILE_ID,
             "target_file_id": {"type": "integer",
                                "description": "Existing owned file to "
                                               "link."},
             "name": {"type": "string",
                      "description": "Filename for a text upload."},
             "content": {"type": "string",
                         "description": "Text content to upload."}},
            required=["file_id"]),
    _tool_attach_file, label="Attached file", module="dashboards")

register_tool(
    "detach_file",
    _schema("dashboards.detach_file",
            "Detach an attachment by attachment id (upload-origin files "
            "are trashed too, linked originals are kept). Refused while "
            "any card references the attachment — unref the cards first "
            "(update_card with the attachment id removed).",
            {"file_id": _FILE_ID,
             "attachment_id": {"type": "string",
                               "description": "Attachment id to detach."}},
            required=["file_id", "attachment_id"]),
    _tool_detach_file, label="Detached file", module="dashboards")

register_tool(
    "run_card",
    _schema("dashboards.run_card",
            "Run one card now and snapshot its result into the dashboard "
            "(prompt cards run the AI agent, workflow cards run the "
            "linked automation). Results write through without "
            "snapshotting history. Refreshing a running card is rejected.",
            {"file_id": _FILE_ID,
             "card_id": {"type": "string"}},
            required=["file_id", "card_id"]),
    _tool_run_card, label="Ran card", module="dashboards")

register_tool(
    "run_all",
    _schema("dashboards.run_all",
            "Run every card on the dashboard sequentially and snapshot "
            "each result. Rejected while any card is still running.",
            {"file_id": _FILE_ID},
            required=["file_id"]),
    _tool_run_all, label="Ran all cards", module="dashboards")

register_tool(
    "recommend",
    _schema("dashboards.recommend",
            "Review a dashboard and propose concrete card edits: starter "
            "cards for empty dashboards, missing prompts, unlinked "
            "workflow cards, missing inputs, failed or stale cards. "
            "Returns recommendations with suggested_edit payloads — "
            "read-only, it changes nothing.",
            {"file_id": _FILE_ID},
            required=["file_id"]),
    _tool_recommend, label="Recommended dashboard changes",
    module="dashboards")
