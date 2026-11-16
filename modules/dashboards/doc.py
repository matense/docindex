"""Dashboard module document service.

A dashboard is a regular StoredFile with the .pdash extension whose blob is
a JSON document::

    {"version": 1, "title": "...", "cards": [
        {"id": "...", "type": "kpi|table|list|chart|markdown|workflow",
         "title": "...", "w": 6, "h": 3, "prompt": "...", "tools": True,
         "script_enabled": False, "script": "...",
         "inputs": {"attachments": [<attId>, ...]}, "result": {...},
         "status": "idle|running|done|error", "ranAt": None}]",
     "attachments": [{"id": "...", "fileId": 12, "name": "incidents.csv",
                      "origin": "upload|link", "addedAt": "..."}],
     "createdFileIds": [34, ...]}

Cards live on a 12-column grid (widths snapped to 1..12 with per-type
minima). Files are attached to the dashboard itself — upload (stored in the
dashboard's drive/folder, dashboard-owned) or link (a reference to an
existing same-user file) — and cards reference attachments by id, never via
free-floating ``file://`` refs. Run-created artifacts are tracked in
``createdFileIds`` so cleanup stays scoped.

Because dashboards are StoredFiles they get ownership, drives/folders,
trash, FileVersion history, FTS search and the AI agent's core tools free.
"""

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO

from flask import g, has_app_context
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import StoredFile, utcnow
from app.services import file_service, indexing_service

EXTENSION = "pdash"
MIME_TYPE = "application/json"

CARD_TYPES = ("kpi", "table", "list", "chart", "markdown", "workflow")

# 12-column grid; card widths are snapped into 1..12.
GRID_COLUMNS = 12
MAX_CARD_HEIGHT = 12

# Per-type minima (width, height) enforced when snapping sizes to the grid.
# KPI cards may shrink to a compact 3x1 single-value row (Task 11).
MIN_SIZE = {
    "kpi": (2, 1),
    "table": (4, 2),
    "list": (3, 2),
    "chart": (4, 3),
    "markdown": (3, 2),
    "workflow": (4, 2),
}

DEFAULT_SIZE = {
    "kpi": (4, 2),
    "table": (6, 3),
    "list": (4, 3),
    "chart": (6, 4),
    "markdown": (6, 3),
    "workflow": (6, 3),
}

TITLE_MAX_CHARS = 255
PROMPT_MAX_CHARS = 20000
SCRIPT_MAX_CHARS = 20000
CARD_STATUSES = ("idle", "running", "done", "error", "interrupted")
ATTACHMENT_ORIGINS = ("upload", "link")

# Per-dashboard run queue depth: how many cards may run at once. Stored on
# the doc so it is configurable upward; the engine holds a per-dashboard
# lock so concurrent POSTs serialize through the cap.
DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 8

# Card keys that change on every run without changing the dashboard's shape.
# save_document() writes these through without snapshotting a FileVersion.
VOLATILE_CARD_KEYS = ("result", "status", "ranAt", "runId", "trace",
                        "ranSecs")

SNAPSHOT_INTERVAL = timedelta(minutes=2)


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------

def new_card(type="kpi", title="", prompt=""):
    if type not in CARD_TYPES:
        raise ValueError(f"Unknown card type '{type}'.")
    w, h = DEFAULT_SIZE[type]
    card = {"id": uuid.uuid4().hex[:12], "type": type,
            "title": title or "", "w": w, "h": h,
            "prompt": prompt or "", "tools": True, "maxSteps": None,
            "script_enabled": False, "script": "",
            "inputs": {"attachments": []},
            "result": None, "status": "idle", "ranAt": None}
    return _validate_card(card)


def empty_dashboard(title="Untitled dashboard"):
    return {"version": 1, "title": title or "Untitled dashboard",
            "cards": [], "attachments": [], "createdFileIds": [],
            "ai_lock": False, "ai_hidden": False}


def _snap_size(card):
    min_w, min_h = MIN_SIZE[card["type"]]
    try:
        w = int(float(card.get("w", min_w)))
    except (TypeError, ValueError):
        raise ValueError(f"Card width must be a number, got "
                         f"{card.get('w')!r}.")
    try:
        h = int(float(card.get("h", min_h)))
    except (TypeError, ValueError):
        raise ValueError(f"Card height must be a number, got "
                         f"{card.get('h')!r}.")
    card["w"] = max(min_w, min(GRID_COLUMNS, w))
    card["h"] = max(min_h, min(MAX_CARD_HEIGHT, h))
    for axis, limit in (("x", GRID_COLUMNS - 1), ("y", None)):
        if axis in card and card[axis] is not None:
            try:
                pos = int(float(card[axis]))
            except (TypeError, ValueError):
                raise ValueError(f"Card {axis} must be a number, got "
                                 f"{card[axis]!r}.")
            pos = max(0, pos)
            card[axis] = min(limit, pos) if limit is not None else pos
    return card


def _validate_card(card, attachment_ids=None):
    if not isinstance(card, dict):
        raise ValueError("Each card must be an object.")
    card.setdefault("id", uuid.uuid4().hex[:12])
    if card.get("type") not in CARD_TYPES:
        raise ValueError(f"Unknown card type '{card.get('type')}'.")
    card["title"] = str(card.get("title") or "")[:TITLE_MAX_CHARS]
    prompt = str(card.get("prompt") or "")
    if len(prompt) > PROMPT_MAX_CHARS:
        raise ValueError(f"Card prompt exceeds the {PROMPT_MAX_CHARS} "
                         f"character cap.")
    card["prompt"] = prompt
    card["tools"] = bool(card.get("tools", True))
    max_steps = card.get("maxSteps")
    if max_steps is None:
        card["maxSteps"] = None
    elif isinstance(max_steps, bool) or not isinstance(max_steps, int) \
            or not 1 <= max_steps <= 128:
        raise ValueError("Card maxSteps must be an integer 1..128 or null.")
    else:
        card["maxSteps"] = max_steps
    script_enabled = bool(card.get("script_enabled", False))
    card["script_enabled"] = script_enabled
    script = card.get("script", "")
    if script is None:
        script = ""
    if not isinstance(script, str):
        raise ValueError("Card script must be text.")
    if len(script) > SCRIPT_MAX_CHARS:
        raise ValueError(f"Card script exceeds the {SCRIPT_MAX_CHARS} "
                         f"character cap.")
    card["script"] = script
    if card["type"] == "workflow" and script_enabled:
        raise ValueError("Workflow cards do not support script mode.")
    inputs = card.get("inputs")
    card["inputs"] = inputs if isinstance(inputs, dict) else {}
    refs = card["inputs"].get("attachments", [])
    if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
        raise ValueError("Card inputs.attachments must be a list of "
                         "attachment ids.")
    card["inputs"]["attachments"] = refs
    if attachment_ids is not None:
        unknown = [r for r in refs if r not in attachment_ids]
        if unknown:
            raise ValueError(f"Card references unknown attachment(s): "
                             f"{', '.join(unknown)}.")
    workflow = card["inputs"].get("workflow")
    if workflow is not None and not isinstance(workflow, dict):
        raise ValueError("Card inputs.workflow must be an object.")
    card.setdefault("status", "idle")
    if card.get("status") not in CARD_STATUSES:
        raise ValueError(f"Unknown card status '{card.get('status')}'.")
    result = card.get("result")
    if result is not None and not isinstance(result, (dict, list, str)):
        raise ValueError("Card result must be an object, list, text or null.")
    trace = card.get("trace")
    if trace is None:
        card["trace"] = None
    elif not isinstance(trace, list):
        raise ValueError("Card trace must be a list or null.")
    ran_at = card.get("ranAt")
    if ran_at is not None:
        card["ranAt"] = str(ran_at)
    ran_secs = card.get("ranSecs")
    if ran_secs is None:
        card["ranSecs"] = None
    elif isinstance(ran_secs, bool) or not isinstance(ran_secs,
                                                      (int, float)):
        raise ValueError("Card ranSecs must be a number of seconds or null.")
    else:
        card["ranSecs"] = float(ran_secs)
    _snap_size(card)
    return card


def _validate_attachment(att):
    if not isinstance(att, dict):
        raise ValueError("Each attachment must be an object.")
    att.setdefault("id", uuid.uuid4().hex[:12])
    if not isinstance(att.get("fileId"), int):
        raise ValueError("Attachment fileId must be an integer.")
    att["name"] = str(att.get("name") or "")[:TITLE_MAX_CHARS]
    if att.get("origin") not in ATTACHMENT_ORIGINS:
        raise ValueError(f"Unknown attachment origin "
                         f"'{att.get('origin')}'.")
    if att.get("addedAt") is not None:
        att["addedAt"] = str(att["addedAt"])
    return att


def _enforce_owned_files(stored, doc):
    """Reject/strip file ids the dashboard owner must not reference.

    Called by save_document() so the ``POST /save`` path cannot persist a
    forged ``{fileId: <another user's id>}``: every attachment entry must
    resolve to a StoredFile row owned by the dashboard owner, otherwise
    ValueError (→400) and nothing is persisted. Same-user trashed files
    are allowed through so the missing-input flow keeps working (a
    trashed attachment persists as a visible "missing input" error at
    run time instead of failing the save).

    ``createdFileIds`` are neutralized by stripping: ids with no
    same-user StoredFile row (foreign or nonexistent) can never be acted
    on (clear/delete resolve same-user only), so they are dropped.
    Same-user ids are kept even when trashed so the artifacts-clear
    "missing" report still sees them.
    """
    for att in doc.get("attachments", []):
        target = db.session.get(StoredFile, att.get("fileId"))
        if target is None or target.user_id != stored.user_id:
            raise ValueError(
                f"Attachment '{att.get('name') or att.get('id')}' "
                f"references file {att.get('fileId')} which is not found "
                f"or not owned by you — attachments must reference your "
                f"own files.")
    owned = []
    for fid in doc.get("createdFileIds", []):
        target = db.session.get(StoredFile, fid)
        if target is not None and target.user_id == stored.user_id:
            if fid not in owned:
                owned.append(fid)
    doc["createdFileIds"] = owned


def validate(doc):
    """Normalize a dashboard document dict; raises ValueError on bad shape."""
    if not isinstance(doc, dict):
        raise ValueError("Document must be an object.")
    attachments = doc.get("attachments")
    if attachments is None:
        attachments = []
    if not isinstance(attachments, list):
        raise ValueError("Document must have an attachments list.")
    doc["attachments"] = [_validate_attachment(a)
                           for a in attachments[:200]]
    attachment_ids = {a["id"] for a in doc["attachments"]}
    cards = doc.get("cards")
    if cards is None:
        cards = []
    if not isinstance(cards, list):
        raise ValueError("Document must have a cards list.")
    doc["cards"] = [_validate_card(c, attachment_ids)
                    for c in cards[:500]]
    created = doc.get("createdFileIds")
    if created is None:
        created = []
    if (not isinstance(created, list)
            or any(not isinstance(f, int) for f in created)):
        raise ValueError("createdFileIds must be a list of file ids.")
    doc["createdFileIds"] = created
    doc["title"] = str(doc.get("title") or "Untitled dashboard"
                       )[:TITLE_MAX_CHARS]
    doc["version"] = 1
    # AI access flags: ai_lock blocks AI edits, ai_hidden blocks all AI
    # access (read, search and listing) to this dashboard.
    doc["ai_lock"] = bool(doc.get("ai_lock"))
    doc["ai_hidden"] = bool(doc.get("ai_hidden"))
    concurrency = doc.get("concurrency", DEFAULT_CONCURRENCY)
    if isinstance(concurrency, bool) or not isinstance(concurrency, int):
        raise ValueError("concurrency must be an integer.")
    if not DEFAULT_CONCURRENCY <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must stay between "
                         f"{DEFAULT_CONCURRENCY} and {MAX_CONCURRENCY}.")
    doc["concurrency"] = concurrency
    return doc


def serialize(doc):
    return json.dumps(doc, ensure_ascii=False, indent=2)


def parse(text):
    try:
        return validate(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupt dashboard file: {exc}") from exc


def load(stored):
    return parse(file_service.read_text_content(
        stored, max_chars=indexing_service.MAX_TEXT_CHARS))


def _result_text(result, _depth=0):
    """Best-effort flattening of a card result to indexable text."""
    if result is None or _depth > 2:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(t for t in
                         (_result_text(v, _depth + 1) for v in result) if t)
    if isinstance(result, dict):
        parts = []
        for value in result.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, (dict, list)):
                nested = _result_text(value, _depth + 1)
                if nested:
                    parts.append(nested)
        return "\n".join(parts)
    return ""


def extract(source):
    """Flatten a dashboard to plain text for the search index.

    Accepts either a StoredFile (indexing pipeline) or an already-loaded
    document dict: titles + prompts + text results + attachment names.
    """
    if isinstance(source, dict):
        doc = source
    else:
        try:
            doc = load(source)
        except (ValueError, OSError):
            return ""
    parts = [doc.get("title", "")]
    for card in doc.get("cards", []):
        parts.append(card.get("title", ""))
        parts.append(card.get("prompt", ""))
        text = _result_text(card.get("result"))
        if text:
            parts.append(text)
    parts.extend(a.get("name", "") for a in doc.get("attachments", []))
    return "\n".join(p for p in parts if p)


def ai_error(stored, write=False):
    """Error message when the AI may not touch this dashboard, else None.

    ai_hidden blocks every access (the dashboard is invisible to the AI);
    ai_lock blocks only mutations. Corrupt/unreadable dashboards are treated
    as accessible — the normal read errors apply.
    """
    try:
        doc = load(stored)
    except (ValueError, OSError):
        return None
    if doc.get("ai_hidden"):
        return ("This dashboard is hidden from the AI by the user — do not "
                "read or mention its content. Tell the user they can unhide "
                "it with the eye button in the dashboard toolbar.")
    if write and doc.get("ai_lock"):
        return ("This dashboard is LOCKED against AI edits by the user. Do "
                "not generate or propose the content in chat — just tell "
                "the user the dashboard is locked and ask them to unlock it "
                "with the lock button in the dashboard toolbar, then repeat "
                "the request.")
    return None


def access_flags(stored):
    """(ai_lock, ai_hidden) booleans, for AI tool listings. Unreadable
    dashboards report no flags."""
    try:
        document = load(stored)
    except (ValueError, OSError):
        return False, False
    return bool(document.get("ai_lock")), bool(document.get("ai_hidden"))


def ai_file_guard(_user, stored):
    """Core guard hook (registered with agent_service): hides hidden
    dashboards from every core AI tool (read, grep, search, list)."""
    if stored.extension != EXTENSION:
        return None
    return ai_error(stored)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def get_dashboard(file_id, user_id):
    """Owned, non-trashed dashboard StoredFile or None."""
    stored = (StoredFile.query
             .filter_by(id=file_id, user_id=user_id, extension=EXTENSION)
             .filter(StoredFile.deleted_at.is_(None))
             .first())
    if stored is not None:
        ensure_uuid(stored)
    return stored


def list_dashboards(user_id):
    dashboards = (StoredFile.query
                  .filter_by(user_id=user_id, extension=EXTENSION)
                  .filter(StoredFile.deleted_at.is_(None))
                  .order_by(StoredFile.updated_at.desc())
                  .all())
    for stored in dashboards:
        ensure_uuid(stored, commit=False)
    if dashboards:
        db.session.commit()
    return dashboards


def ensure_uuid(stored, commit=True):
    """Backfill the internal global identifier (pre-UUID rows only)."""
    if not stored.uuid:
        stored.uuid = str(uuid.uuid4())
        if commit:
            db.session.commit()
    return stored.uuid


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _guard_writable(stored):
    from app.services.file_service import is_readonly_file
    if is_readonly_file(stored):
        raise ValueError("Synced drives are read-only.")


def create_dashboard(user, drive, title, folder=None):
    """Create a dashboard file in a writable drive. Returns StoredFile."""
    if isinstance(drive, (list, tuple)):
        candidates = list(drive)
        drive = next((d for d in candidates
                      if not getattr(d, "is_readonly", False)), None)
        if drive is None:
            raise ValueError("All drives in context are synced (read-only) — "
                             "pick a writable drive.")
    _guard_writable_drive(drive)
    clean = (title or "").strip() or "Untitled dashboard"
    doc = empty_dashboard(clean)
    validate(doc)
    data = serialize(doc).encode("utf-8")
    storage = FileStorage(stream=BytesIO(data),
                          filename=f"{clean}.{EXTENSION}",
                          content_type=MIME_TYPE)
    stored = file_service.save_upload(storage, user, folder=folder,
                                      drive=drive)
    stored.drive_id = drive.id
    stored.uuid = ensure_uuid(stored, commit=False)
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    return stored


def _guard_writable_drive(drive):
    if isinstance(drive, (list, tuple)):
        raise ValueError("A single target drive is required — pick one with "
                         "drive_id.")
    if drive.is_readonly:
        raise ValueError("Synced drives are read-only — dashboards cannot be "
                         "created there.")


def _owner(user_id):
    from app.models import User
    user = db.session.get(User, user_id)
    if user is None:
        raise ValueError("Dashboard owner not found.")
    return user


def attach_upload(stored, file_storage):
    """Attach an uploaded file to the dashboard.

    The file is stored in the dashboard's own drive/folder (dashboard-owned)
    and registered as an ``origin: "upload"`` attachment.
    """
    asset = file_service.save_upload(file_storage, _owner(stored.user_id),
                                     folder=stored.folder,
                                     drive=stored.drive)
    asset.drive_id = stored.drive_id
    db.session.commit()
    indexing_service.enqueue_index([asset.id])
    att = {"id": uuid.uuid4().hex[:12], "fileId": asset.id,
           "name": asset.name, "origin": "upload",
           "addedAt": utcnow().isoformat()}
    doc = load(stored)
    doc["attachments"].append(_validate_attachment(att))
    save_document(stored, doc)
    return att


def attach_link(stored, file_id):
    """Attach a reference to an existing file (same-user owned).

    Repeat links dedupe: linking an already-attached file returns the
    existing entry instead of adding a second one.
    """
    target = (StoredFile.query
              .filter_by(id=file_id, user_id=stored.user_id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if target is None:
        raise ValueError(f"File {file_id} not found or not owned by you — "
                         f"attachments must reference your own files.")
    doc = load(stored)
    for existing in doc.get("attachments", []):
        if existing.get("fileId") == target.id:
            return existing
    att = {"id": uuid.uuid4().hex[:12], "fileId": target.id,
           "name": target.name, "origin": "link",
           "addedAt": utcnow().isoformat()}
    doc["attachments"].append(_validate_attachment(att))
    save_document(stored, doc)
    return att


def detach_attachment(stored, att_id):
    """Remove an attachment; upload-origin also trashes the owned file.

    Refused while any card still references the attachment — unref the
    cards first. Returns ``{"removed_file": bool}`` — True only when an
    upload-origin file was actually trashed.
    """
    doc = load(stored)
    entry = next((a for a in doc.get("attachments", [])
                  if a.get("id") == att_id), None)
    if entry is None:
        raise ValueError(f"Attachment '{att_id}' not found.")
    users = [c.get("title") or c.get("type", "?") for c in
             doc.get("cards", [])
             if att_id in ((c.get("inputs") or {}).get("attachments")
                           or [])]
    if users:
        raise ValueError(
            f"Cannot detach '{entry.get('name') or att_id}': still used "
            f"by card(s) {', '.join(users)} — remove the reference from "
            f"the card(s) first.")
    doc["attachments"] = [a for a in doc["attachments"]
                          if a.get("id") != att_id]
    for card in doc.get("cards", []):
        refs = (card.get("inputs") or {}).get("attachments")
        if isinstance(refs, list) and att_id in refs:
            card["inputs"]["attachments"] = [r for r in refs
                                             if r != att_id]
    removed_file = False
    if entry.get("origin") == "upload":
        target = (StoredFile.query
                  .filter_by(id=entry.get("fileId"),
                             user_id=stored.user_id)
                  .filter(StoredFile.deleted_at.is_(None))
                  .first())
        if target is not None:
            file_service.delete_file(target)
            removed_file = True
    save_document(stored, doc)
    return {"removed_file": removed_file}


# ---------------------------------------------------------------------------
# Artifact registry and cleanup
# ---------------------------------------------------------------------------

def register_artifact(stored, file_id):
    """Track a run-created file so cleanup stays scoped.

    Returns True when the id was recorded; False for garbage (non-integer,
    missing, trashed or foreign files are never registered).
    """
    try:
        fid = int(file_id)
    except (TypeError, ValueError):
        return False
    target = (StoredFile.query
              .filter_by(id=fid, user_id=stored.user_id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if target is None:
        return False
    doc = load(stored)
    if fid not in doc.get("createdFileIds", []):
        doc["createdFileIds"].append(fid)
        save_document(stored, doc)
    return True


def _resolve_files(stored, file_ids):
    """Split file ids into existing vs gone (trashed, moved or deleted).

    Returns ([{"id", "name"}], [{"id", "name"|None, "missing": True}]).
    Only same-user files are ever listed; foreign ids read as missing.
    """
    found, missing = [], []
    for fid in file_ids:
        target = db.session.get(StoredFile, fid)
        if (target is not None and target.user_id == stored.user_id
                and target.deleted_at is None):
            found.append({"id": target.id, "name": target.name})
        else:
            name = (target.name if target is not None
                    and target.user_id == stored.user_id else None)
            missing.append({"id": fid, "name": name, "missing": True})
    return found, missing


def _attachment_files(doc, origin):
    return [a.get("fileId") for a in doc.get("attachments", [])
            if a.get("origin") == origin and isinstance(a.get("fileId"), int)]


def delete_info(stored):
    """Inventory for the delete confirm modal: what removing the dashboard
    deletes (dashboard + owned uploads + run-created artifacts) and what it
    keeps (linked originals, listed so the user sees they are untouched)."""
    doc = load(stored)
    uploads, _ = _resolve_files(
        stored, _attachment_files(doc, "upload"))
    artifacts, _ = _resolve_files(stored, doc.get("createdFileIds", []))
    linked, _ = _resolve_files(stored, _attachment_files(doc, "link"))
    return {"dashboard": {"id": stored.id, "name": stored.name},
            "ownedUploads": uploads,
            "artifacts": artifacts,
            "linkedKept": linked}


def clear_artifacts(stored):
    """Trash run-created artifacts; keep the dashboard and its attachments.

    Returns {"cleared": [ids], "missing": [ids]} — missing covers files that
    were already gone (trashed, moved or deleted outside the dashboard).
    """
    _guard_writable(stored)
    doc = load(stored)
    cleared, missing = [], []
    for fid in list(doc.get("createdFileIds", [])):
        target = (StoredFile.query
                  .filter_by(id=fid, user_id=stored.user_id)
                  .filter(StoredFile.deleted_at.is_(None))
                  .first())
        if target is None:
            missing.append(fid)
            continue
        file_service.delete_file(target)
        cleared.append(fid)
    doc["createdFileIds"] = []
    save_document(stored, doc)
    return {"cleared": cleared, "missing": missing}


def delete_dashboard(stored):
    """Remove the dashboard plus owned uploads and run-created artifacts.

    Linked originals are never touched. Everything is trashed (blobs kept),
    mirroring the notebooks precedent. Returns the inventory of what was
    removed and what was kept.
    """
    _guard_writable(stored)
    info = delete_info(stored)
    removed_uploads, removed_artifacts = [], []
    for entry in info["ownedUploads"]:
        target = db.session.get(StoredFile, entry["id"])
        if target is not None and target.deleted_at is None:
            file_service.delete_file(target)
            removed_uploads.append(entry["id"])
    for entry in info["artifacts"]:
        target = db.session.get(StoredFile, entry["id"])
        if target is not None and target.deleted_at is None:
            file_service.delete_file(target)
            removed_artifacts.append(entry["id"])
    file_service.delete_file(stored)
    return {"dashboard": info["dashboard"],
            "removedUploads": removed_uploads,
            "removedArtifacts": removed_artifacts,
            "linkedKept": info["linkedKept"]}


# ---------------------------------------------------------------------------
# Run recovery
# ---------------------------------------------------------------------------

def recover_interrupted(stored):
    """Mark cards stuck in `running` as interrupted (automation
    `recover_interrupted` precedent).

    Runs on dashboard load: a `running` card with no live worker behind it
    belongs to a dead process (e.g. a server restart mid-run). The rewrite
    only touches volatile keys, so it writes through without snapshotting.
    Returns the number of cards recovered.
    """
    try:
        doc = load(stored)
    except (ValueError, OSError):
        return 0
    fixed = 0
    for card in doc.get("cards", []):
        if card.get("status") == "running" and not is_run_live(
                stored.id, card.get("runId")):
            card["status"] = "interrupted"
            card["result"] = {
                "error": "This run was interrupted (the dashboard was "
                         "reloaded while the run was active). "
                         "Run the card again."}
            fixed += 1
    if fixed:
        save_document(stored, doc)
    return fixed


_live_guard = threading.Lock()
# Live runs with their start monotonic, keyed by (dashboard_id, run_id).
# Same single-worker assumption as the engine's per-dashboard locks:
# one process holds all threads, and a server restart clears the set —
# after a restart every stuck `running` card is genuinely dead, so
# recover_interrupted still does its job.
_live_runs = {}


def run_started(file_id, run_id):
    """Register a live run; reloads must spare its card."""
    with _live_guard:
        _live_runs[(file_id, run_id)] = time.monotonic()


def run_finished(file_id, run_id):
    """Drop a finished run from the live set (unknown ids are no-ops)."""
    with _live_guard:
        _live_runs.pop((file_id, run_id), None)


def is_run_live(file_id, run_id):
    """Whether run_id on file_id currently has a live worker."""
    with _live_guard:
        return (file_id, run_id) in _live_runs


def run_elapsed(file_id, run_id):
    """Seconds since the run started (one decimal), or None if unknown."""
    with _live_guard:
        start = _live_runs.get((file_id, run_id))
    if start is None:
        return None
    return round(time.monotonic() - start, 1)


# Run-owned card keys: written by background runs, never user intent.
# A stale Builder full-doc save must never wipe these — merge_run_owned()
# keeps the newer side's values (D1 per-field-merge precedent).
RUN_OWNED_CARD_KEYS = ("result", "status", "ranAt", "trace", "runId",
                       "ranSecs")


def _ran_at_newer(server_ran, incoming_ran):
    """Whether the server card's run is strictly newer than the incoming
    snapshot's. ranAt is utcnow().isoformat(); parsed comparison first,
    lexicographic fallback for foreign shapes."""
    if not server_ran:
        return False
    if not incoming_ran:
        return True
    try:
        return (datetime.fromisoformat(str(server_ran))
                > datetime.fromisoformat(str(incoming_ran)))
    except (ValueError, TypeError):
        return str(server_ran) > str(incoming_ran)


def _server_run_wins(server, incoming):
    """Whether the live card's run-owned fields beat the incoming snapshot.

    A live running state always beats a non-running snapshot (the status
    poll and run-status endpoint read it mid-run). Both sides running with
    different runIds means the snapshot belongs to an older run — the live
    run wins. Same runId on both sides falls through to the ranAt compare.
    Intent: the decision is card-level, not field-level — run fields are
    written atomically by the settle path, so they move as a unit. A
    strictly newer incoming ranAt wins for the incoming side (symmetric
    newer-wins; unreachable from the Builder today, which only ever holds
    older snapshots). An exact ranAt tie falls to incoming (same run or
    neither side ever ran — nothing newer to protect).
    """
    if server.get("status") == "running":
        if incoming.get("status") != "running":
            return True
        if server.get("runId") != incoming.get("runId"):
            return True
    return _ran_at_newer(server.get("ranAt"), incoming.get("ranAt"))


def merge_run_owned(current_doc, incoming_doc, union_cards=False):
    """Fold newer background-run state into a stale full-doc save.

    For each card present on both sides, the newer side's run-owned keys
    (result, status, ranAt, trace, runId, ranSecs) win; structural keys
    (title, prompt, sizes, inputs, ...) always follow the incoming save
    (user intent). createdFileIds is an append-only registry — unioned
    (incoming order first), since a stale snapshot can only lack newer
    ids, never intentionally remove them (clearing goes only through
    clear_artifacts, never through full-doc saves).

    Server-only cards (added concurrently, e.g. by an AI tool or another
    tab) are union-preserved at their live positions (D1 cells precedent)
    when union_cards is True. The caller sets it only on checksum mismatch
    — proof someone else wrote since the snapshot — so plain user card
    deletions (checksums match) stand. A missing base checksum means no
    evidence either way: membership follows the incoming save.
    """
    server_by_id = {c.get("id"): c for c in current_doc.get("cards", [])}
    for card in incoming_doc.get("cards", []):
        server = server_by_id.get(card.get("id"))
        if server is None:
            continue
        if _server_run_wins(server, card):
            for key in RUN_OWNED_CARD_KEYS:
                if key in server:
                    card[key] = copy.deepcopy(server[key])
                else:
                    card.pop(key, None)
    if union_cards:
        present = {c.get("id") for c in incoming_doc.get("cards", [])}
        merged = list(incoming_doc.get("cards", []))
        for index, card in enumerate(current_doc.get("cards", [])):
            if card.get("id") not in present:
                merged.insert(min(index, len(merged)), copy.deepcopy(card))
                present.add(card.get("id"))
        incoming_doc["cards"] = merged
    seen, merged_ids = set(), []
    for fid in (list(incoming_doc.get("createdFileIds", []))
                + list(current_doc.get("createdFileIds", []))):
        if fid not in seen:
            seen.add(fid)
            merged_ids.append(fid)
    incoming_doc["createdFileIds"] = merged_ids
    return incoming_doc


def _structural_shape(doc):
    """The dashboard minus per-run volatile card keys — structural edits
    change this shape, result writes do not."""
    shape = {"title": doc.get("title"),
             "attachments": doc.get("attachments", []),
             "createdFileIds": doc.get("createdFileIds", []),
             "concurrency": doc.get("concurrency", DEFAULT_CONCURRENCY)}
    cards = []
    for card in doc.get("cards", []):
        cards.append({k: v for k, v in card.items()
                      if k not in VOLATILE_CARD_KEYS})
    shape["cards"] = cards
    return shape


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_blob(stored, doc):
    data = serialize(doc)
    path = file_service.file_path(stored)
    with file_service.file_lock(stored.id):
        tmp_path = f"{path}.tmp-{uuid.uuid4().hex}"
        with open(tmp_path, "wb") as fh:
            fh.write(data.encode("utf-8"))
        if hashlib.sha256(data.encode("utf-8")).hexdigest() != _sha256_file(
                tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise ValueError("Dashboard write failed verification.")
        os.replace(tmp_path, path)
    stored.size = os.path.getsize(path)
    stored.checksum = hashlib.sha256(data.encode("utf-8")).hexdigest()
    stored.updated_at = utcnow()
    db.session.commit()
    indexing_service.enqueue_index([stored.id])
    file_service.refresh_shadow(stored)


def save_document(stored, doc, force_checkpoint=False, note="",
                  source="edit", skip_snapshot=False):
    """Persist a dashboard: snapshot on structure, write-through on results.

    Card runs write results through immediately (no data loss) but never
    snapshot a FileVersion for result-only changes, so frequent refreshes
    never explode version storage. Structural edits (layout, cards, prompts,
    inputs, attachments) snapshot like notebooks: throttled to one version
    per SNAPSHOT_INTERVAL unless force_checkpoint=True (explicit save).
    Returns True when a version was snapshotted.
    """
    _guard_writable(stored)
    validate(doc)
    _enforce_owned_files(stored, doc)
    try:
        old_shape = _structural_shape(load(stored))
    except (ValueError, OSError):
        old_shape = None
    results_only = (old_shape is not None
                    and old_shape == _structural_shape(doc))
    if results_only and not force_checkpoint and not skip_snapshot:
        _write_blob(stored, doc)
        return False

    latest = stored.versions[0] if stored.versions else None
    snapshotted = False
    last_snapshot_at = latest.created_at if latest else None
    if last_snapshot_at is not None and last_snapshot_at.tzinfo is None:
        last_snapshot_at = last_snapshot_at.replace(tzinfo=UTC)
    if (not skip_snapshot
            and (force_checkpoint or last_snapshot_at is None
                 or utcnow() - last_snapshot_at > SNAPSHOT_INTERVAL)):
        file_service.snapshot_version(stored, source, note=note)
        snapshotted = True
    _write_blob(stored, doc)
    return snapshotted


def save_document_ai(stored, doc, note="AI edits"):
    """Save from an AI agent run, transactionally: the first mutation of a
    dashboard within the current request snapshots the pre-edit state, and
    every later mutation in the same run just writes through. One AI answer
    therefore leaves a single restorable history block instead of one
    version per tool call (notebooks ``save_document_ai`` precedent)."""
    first_in_run = True
    if has_app_context():
        # g is request/app-context scoped: one chat message = one run.
        snapped = getattr(g, "_dash_ai_snapshotted", None)
        if snapped is None:
            snapped = set()
            g._dash_ai_snapshotted = snapped
        first_in_run = stored.id not in snapped
        snapped.add(stored.id)
    return save_document(stored, doc, force_checkpoint=True,
                         skip_snapshot=not first_in_run,
                         note=note, source="ai")
