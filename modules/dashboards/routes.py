"""Dashboard module routes: list, editor, save, attachments, history."""

import difflib
import json
import time
import uuid

from flask import (Blueprint, Response, abort, flash, jsonify, redirect,
                   render_template, request, stream_with_context, url_for)
from flask_login import current_user

from app.extensions import db
from app.models import Drive, Folder, StoredFile, User, utcnow
from app.services import file_service, indexing_service

from . import doc as doc_service
from . import engine as engine_service

bp = Blueprint("dashboards", __name__,
               template_folder="templates", static_folder="static")


def _get_dashboard(file_id):
    stored = doc_service.get_dashboard(file_id, current_user.id)
    if stored is None:
        abort(404)
    return stored


def _guard_writable(stored):
    try:
        doc_service._guard_writable(stored)
    except ValueError as exc:
        abort(400, str(exc))


def _locations():
    """(value, label) choices for the create form: roots + folders of every
    non-synced drive owned by the user."""
    choices = []
    drives = (Drive.query
              .filter_by(user_id=current_user.id)
              .order_by(Drive.name).all())
    for drive in drives:
        if drive.is_synced:
            continue
        choices.append((f"drive:{drive.id}", f"{drive.name} /"))
        folders = (Folder.query
                   .filter_by(user_id=current_user.id, drive_id=drive.id)
                   .order_by(Folder.name).all())
        for folder in folders:
            label = " / ".join(f.name for f in folder.breadcrumb())
            choices.append((f"folder:{folder.id}",
                            f"{drive.name} / {label}"))
    return choices


@bp.get("/")
def index():
    dashboards = doc_service.list_dashboards(current_user.id)
    enriched = [{"file": stored,
                 "cards": len(doc_service.load(stored)["cards"])
                 if _readable(stored) else 0}
                for stored in dashboards]
    return render_template("dashboards/list.html",
                           dashboards=enriched,
                           locations=_locations())


def _readable(stored):
    try:
        doc_service.load(stored)
        return True
    except (ValueError, OSError):
        return False


def _lookup(model, raw):
    """Fetch a Drive/Folder by id string: malformed ids abort 400, unknown
    ids return None (the caller turns an explicit miss into a 404)."""
    try:
        key = int(raw)
    except (TypeError, ValueError):
        abort(400, "Invalid location.")
    return db.session.get(model, key)


@bp.post("/new")
def new():
    name = (request.form.get("name")
            or request.form.get("title")
            or "Untitled dashboard").strip()
    drive, folder = None, None
    explicit = False
    missing = False
    location = (request.form.get("location") or "").strip()
    if location.startswith("drive:"):
        explicit = True
        drive = _lookup(Drive, location[6:])
        missing = drive is None
    elif location.startswith("folder:"):
        explicit = True
        folder = _lookup(Folder, location[7:])
        missing = folder is None
        drive = folder.drive if folder is not None else None
    elif location:
        abort(400, "Invalid location.")
    else:
        raw_drive = (request.form.get("drive_id") or "").strip()
        raw_folder = (request.form.get("folder_id") or "").strip()
        if raw_drive:
            explicit = True
            drive = _lookup(Drive, raw_drive)
            missing = missing or drive is None
        if raw_folder:
            explicit = True
            folder = _lookup(Folder, raw_folder)
            missing = missing or folder is None
            if folder is not None:
                drive = folder.drive
    if explicit and (missing or drive is None):
        # An explicit target that does not exist (or a folder outside any
        # drive) — never fall through to the default drive.
        abort(404)
    if drive is None:
        # No explicit target: first writable drive owned by the user.
        drive = (Drive.query
                 .filter_by(user_id=current_user.id)
                 .order_by(Drive.name).all())
        drive = next((d for d in drive if not d.is_synced), None)
    if drive is None or drive.user_id != current_user.id:
        abort(404)
    if folder is not None and folder.user_id != current_user.id:
        abort(404)
    try:
        stored = doc_service.create_dashboard(current_user, drive, name,
                                              folder=folder)
    except ValueError as exc:
        abort(400, str(exc))
    return redirect(url_for("dashboards.editor", file_id=stored.id))


@bp.get("/<int:file_id>")
def editor(file_id):
    stored = _get_dashboard(file_id)
    doc_service.recover_interrupted(stored)
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        abort(400, str(exc))
    return render_template("dashboards/edit.html", file=stored, doc=document)


@bp.get("/<int:file_id>/doc")
def document_json(file_id):
    """JSON document + timestamp — used by the editor to pick up external
    (e.g. AI tool) changes while the dashboard is open."""
    stored = _get_dashboard(file_id)
    doc_service.recover_interrupted(stored)
    try:
        document = doc_service.load(stored)
    except ValueError as exc:
        abort(400, str(exc))
    return jsonify({"ok": True, "doc": document,
                    "checksum": stored.checksum,
                    "updated_at": (stored.updated_at.isoformat()
                                   if stored.updated_at else None)})


@bp.post("/<int:file_id>/save")
def save(file_id):
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    payload = request.get_json(silent=True) or {}
    document = payload.get("doc")
    if document is None:
        return jsonify({"ok": False, "error": "Missing document."}), 400
    try:
        # A stale Builder snapshot must not wipe background-run state that
        # landed after the snapshot (D3 per-field merge, newer wins). The
        # Builder sends the checksum it last saw; a mismatch proves someone
        # else wrote since, so concurrently added cards union in too (D1
        # precedent) — matching checksums mean the snapshot is merely
        # user-edited and card membership (incl. user deletes) stands.
        # A corrupt live blob skips the merge — the save overwrites it as
        # before.
        # The load + merge + write section holds the dashboard lock (same
        # shape as the run_cards admission lock below): a run settling
        # between the load and the write would otherwise be wiped.
        base_checksum = payload.get("base_checksum")
        with engine_service.dashboard_lock(stored.id):
            try:
                current = doc_service.load(stored)
            except (ValueError, OSError):
                current = None
            if current is not None:
                document = doc_service.merge_run_owned(
                    current, document,
                    union_cards=(base_checksum is not None
                                 and base_checksum != stored.checksum))
            doc_service.save_document(
                stored, document,
                force_checkpoint=bool(payload.get("force_checkpoint")))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True,
                    "saved_at": stored.updated_at.isoformat(),
                    "checksum": stored.checksum})


@bp.post("/<int:file_id>/attachments")
def attachments(file_id):
    """Attach a file: multipart upload (dashboard-owned) or {"file_id": N}
    link to an existing owned file. Returns the attachment entry."""
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    upload = request.files.get("file")
    if upload is not None and upload.filename:
        try:
            att = doc_service.attach_upload(stored, upload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "attachment": att})
    payload = request.get_json(silent=True) or {}
    target_id = payload.get("file_id") or request.form.get("file_id")
    if target_id is None:
        return jsonify({"ok": False, "error": "Missing file."}), 400
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid file id."}), 400
    try:
        att = doc_service.attach_link(stored, target_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "attachment": att})


@bp.delete("/<int:file_id>/attachments/<att_id>")
def detach_attachment(file_id, att_id):
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    try:
        result = doc_service.detach_attachment(stored, att_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


# ---------------------------------------------------------------------------
# Browse: drive/folder tree + quick search for the attach picker (Task 8)
# ---------------------------------------------------------------------------
# JSON versions of the Drive tree lazy-expansion pattern
# (app/routes/drive.py:tree_children): one level per request, ownership
# checks via the current user, 404 on foreign drives/folders.

BROWSE_SEARCH_CAP = 20


def _browse_drive(raw_drive_id):
    """Own, non-trashed drive or abort: malformed ids 400, foreign/missing
    drives 404 (same standard as the attach_link ownership rule)."""
    try:
        drive_id = int(raw_drive_id)
    except (TypeError, ValueError):
        abort(400, "Invalid drive.")
    drive = db.session.get(Drive, drive_id)
    if (drive is None or drive.user_id != current_user.id
            or drive.deleted_at is not None):
        abort(404)
    return drive


def _browse_file_entry(stored):
    return {"id": stored.id, "name": stored.name,
            "mime": stored.mime_type, "size": stored.size}


@bp.get("/browse/drives")
def browse_drives():
    """Drives the picker may browse: own, non-trashed, by name."""
    drives = (Drive.query
              .filter_by(user_id=current_user.id)
              .filter(Drive.deleted_at.is_(None))
              .order_by(Drive.name).all())
    return jsonify([{"id": drive.id, "name": drive.name}
                    for drive in drives])


@bp.get("/browse/children")
def browse_children():
    """One tree level: ``?drive_id=`` plus ``parent_id=`` (root when absent
    or ``null``). Folders carry ``hasKids`` for the lazy expand arrows;
    trashed files and folders are excluded. 404 on foreign/unowned,
    trashed, or cross-drive folder."""
    drive = _browse_drive(request.args.get("drive_id"))
    raw_parent = request.args.get("parent_id")
    parent = None
    if raw_parent not in (None, "", "null", "None"):
        try:
            parent_id = int(raw_parent)
        except (TypeError, ValueError):
            abort(400, "Invalid folder.")
        parent = db.session.get(Folder, parent_id)
        if (parent is None or parent.user_id != current_user.id
                or parent.drive_id != drive.id
                or parent.deleted_at is not None):
            abort(404)
    parent_id = parent.id if parent is not None else None
    folders = (Folder.query
               .filter_by(user_id=current_user.id, drive_id=drive.id,
                          parent_id=parent_id)
               .filter(Folder.deleted_at.is_(None))
               .order_by(Folder.name).all())
    files = (StoredFile.query
             .filter_by(user_id=current_user.id, drive_id=drive.id,
                        folder_id=parent_id)
             .filter(StoredFile.deleted_at.is_(None))
             .order_by(StoredFile.name).all())
    # hasKids without N+1: folders below any listed folder, or a visible
    # file directly inside it (same grouped-query shape as _tree_nonempty).
    folder_ids = [folder.id for folder in folders]
    nonempty = set()
    if folder_ids:
        nonempty |= {row[0] for row in db.session.query(Folder.parent_id)
                     .filter(Folder.user_id == current_user.id,
                             Folder.drive_id == drive.id,
                             Folder.parent_id.in_(folder_ids),
                              Folder.deleted_at.is_(None))
                     .distinct().all()}
        nonempty |= {row[0] for row in db.session.query(StoredFile.folder_id)
                     .filter(StoredFile.user_id == current_user.id,
                             StoredFile.drive_id == drive.id,
                             StoredFile.folder_id.in_(folder_ids),
                             StoredFile.deleted_at.is_(None))
                     .distinct().all()}
    return jsonify({
        "folders": [{"id": folder.id, "name": folder.name,
                     "hasKids": folder.id in nonempty}
                    for folder in folders],
        "files": [_browse_file_entry(stored) for stored in files],
    })


@bp.get("/browse/search")
def browse_search():
    """Name match over the user's visible files for the picker's quick
    search. Files in trashed drives or trashed folders are excluded.
    Same file shape as children, capped at 20, 400 on empty query."""
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Missing query."}), 400
    escaped = (query.replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    files = (StoredFile.query
             .filter_by(user_id=current_user.id)
             .filter(StoredFile.deleted_at.is_(None),
                     StoredFile.name.ilike(f"%{escaped}%", escape="\\"))
             .outerjoin(Drive, StoredFile.drive_id == Drive.id)
             .filter((Drive.id.is_(None))
                     | (Drive.deleted_at.is_(None)))
             .outerjoin(Folder, StoredFile.folder_id == Folder.id)
             .filter((Folder.id.is_(None))
                     | (Folder.deleted_at.is_(None)))
             .order_by(StoredFile.name)
             .limit(BROWSE_SEARCH_CAP).all())
    return jsonify({"files": [_browse_file_entry(stored)
                               for stored in files]})


@bp.get("/<int:file_id>/history")
def history(file_id):
    stored = _get_dashboard(file_id)
    return render_template("dashboards/history.html", file=stored,
                           versions=stored.versions)


def _card_text(card):
    """Human-readable text of a card, used for diffing."""
    lines = [f"[{card.get('type', '?')}] {card.get('title', '')}"]
    if card.get("prompt"):
        lines.append(str(card["prompt"]))
    text = doc_service._result_text(card.get("result"))
    if text:
        lines.append(text)
    return "\n".join(lines)


def _side_by_side_rows(old_text, new_text):
    """Pair old/new lines for a side-by-side view using SequenceMatcher
    opcodes. Each row: {left, right, cls} where cls is same/del/add and a
    side is None when the line only exists on the other side."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    rows = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            run = i2 - i1
            if run > 6:
                # Collapse long unchanged stretches to keep the diff short.
                for k in range(3):
                    rows.append({"left": old_lines[i1 + k],
                                 "right": new_lines[j1 + k], "cls": "same"})
                rows.append({"collapse": run - 6})
                for k in range(i2 - 3, i2):
                    rows.append({"left": old_lines[k],
                                 "right": new_lines[j2 - (i2 - k)],
                                 "cls": "same"})
            else:
                for k in range(run):
                    rows.append({"left": old_lines[i1 + k],
                                 "right": new_lines[j1 + k], "cls": "same"})
        else:
            if tag in ("delete", "replace"):
                for line in old_lines[i1:i2]:
                    rows.append({"left": line, "right": None, "cls": "del"})
            if tag in ("insert", "replace"):
                for line in new_lines[j1:j2]:
                    rows.append({"left": None, "right": line, "cls": "add"})
    return rows


def _card_diff(old_doc, new_doc):
    """Align cards by id and classify each as added / changed / removed.

    Returns (summary, entries); every entry carries side-by-side `rows`
    (left = old version, right = current). Removed cards come last, since
    they no longer have a position in the document. Result-only changes
    (result/status/ranAt/runId) never snapshot, so they read as unchanged.
    """
    old_by_id = {c.get("id"): (i, c)
                 for i, c in enumerate(old_doc.get("cards", []))}
    entries = []
    added = changed = 0
    for i, card in enumerate(new_doc.get("cards", [])):
        label = f"{card.get('type', '?')} · card {i + 1}"
        prev = old_by_id.pop(card.get("id"), None)
        if prev is None:
            added += 1
            entries.append({"kind": "added", "label": label,
                            "rows": _side_by_side_rows("", _card_text(card))})
            continue
        _old_i, old_card = prev
        old_text, new_text = _card_text(old_card), _card_text(card)
        if (old_card.get("type") == card.get("type")
                and old_text == new_text
                and _structural_card(old_card) == _structural_card(card)):
            continue
        changed += 1
        rows = _side_by_side_rows(old_text, new_text)
        if not any(r.get("cls", "same") != "same" for r in rows):
            # Only settings changed (size, position, tools toggle, ...).
            rows = [{"left": "~ card settings changed",
                     "right": "~ card settings changed", "cls": "same"}]
        entries.append({"kind": "changed", "label": label, "rows": rows})
    removed = 0
    for old_i, old_card in old_by_id.values():
        removed += 1
        entries.append({"kind": "removed",
                        "label": f"{old_card.get('type', '?')} · "
                                 f"was card {old_i + 1}",
                        "rows": _side_by_side_rows(
                            _card_text(old_card), "")})
    summary = {"added": added, "changed": changed, "removed": removed,
               "unchanged": len(new_doc.get("cards", [])) - added - changed,
               "title_changed": old_doc.get("title") != new_doc.get("title"),
               "old_title": old_doc.get("title", ""),
               "new_title": new_doc.get("title", "")}
    return summary, entries


def _structural_card(card):
    return {k: v for k, v in card.items()
            if k not in doc_service.VOLATILE_CARD_KEYS and k != "id"}


@bp.get("/<int:file_id>/history/<int:version_id>/diff")
def history_diff(file_id, version_id):
    stored = _get_dashboard(file_id)
    version = next((v for v in stored.versions if v.id == version_id), None)
    if version is None:
        abort(404)
    with open(file_service.version_path(version),
              encoding="utf-8", errors="replace") as fh:
        old_doc = doc_service.parse(fh.read())
    summary, entries = _card_diff(old_doc, doc_service.load(stored))
    return render_template("dashboards/diff.html", file=stored,
                           version=version, summary=summary, entries=entries)


@bp.post("/<int:file_id>/history/<int:version_id>/restore")
def history_restore(file_id, version_id):
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    version = next((v for v in stored.versions if v.id == version_id), None)
    if version is None:
        abort(404)
    file_service.restore_version(stored, version)
    indexing_service.enqueue_index([stored.id])
    flash("Dashboard restored.", "success")
    return redirect(url_for("dashboards.editor", file_id=stored.id))


# ---------------------------------------------------------------------------
# Execution: run cards, poll status
# ---------------------------------------------------------------------------

def _busy(already_running):
    return jsonify({"ok": False,
                    "error": "Another run is already in progress on this "
                             "dashboard — wait for it to finish, then try "
                             "again.",
                    "already_running": already_running}), 409


def _fresh_card(stored, card_id):
    doc = doc_service.load(stored)
    card = next((c for c in doc.get("cards", [])
                 if c.get("id") == card_id), None)
    if card is None:
        raise ValueError(f"Unknown card '{card_id}'.")
    return card


@bp.post("/<int:file_id>/run")
def run_cards(file_id):
    """Run one card (``{"card_id": ...}``) or the whole dashboard
    (``{"all": true}``). Admission (cap check + mark running) holds the
    dashboard lock; the lock is released for the LLM/workflow wait and
    re-acquired per card to settle, so runs admitted under the cap overlap
    instead of blocking on the whole execution. Returns
    ``{"accepted": [run_ids]}``. A run targeting a card that is already
    running (or exceeding the cap) is rejected with 409 + ``already_running``
    instead of queueing."""
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    payload = request.get_json(silent=True) or {}
    try:
        doc = doc_service.load(stored)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if payload.get("all"):
        targets = [c.get("id") for c in doc.get("cards", [])]
    elif payload.get("card_id"):
        targets = [payload["card_id"]]
    else:
        return jsonify({"ok": False,
                        "error": "Pass card_id or all: true."}), 400
    known = {c.get("id") for c in doc.get("cards", [])}
    unknown = [t for t in targets if t not in known]
    if unknown:
        return jsonify({"ok": False,
                        "error": f"Unknown card(s): "
                                 f"{', '.join(unknown)}."}), 404

    # Admission (cap check + mark running) holds the lock; the lock is
    # released for the LLM/workflow wait and re-acquired per card to settle
    # (run_card_stream shape), so admitted runs overlap. Settle sections
    # stay serialized by the lock and guarded by run-id, so concurrent
    # completions never clobber each other.
    with engine_service.dashboard_lock(stored.id):
        doc = doc_service.load(stored)
        # Re-validate targets against the reloaded doc: a card deleted
        # between the pre-lock check and this reload is gone — a single
        # missing target 404s (same shape as above); a run-all simply
        # skips the missing cards so the rest still proceed (never a
        # bare next() StopIteration 500).
        live = {c.get("id") for c in doc.get("cards", [])}
        missing = [t for t in targets if t not in live]
        if missing and not payload.get("all"):
            return jsonify({"ok": False,
                            "error": f"Unknown card(s): "
                                     f"{', '.join(missing)}."}), 404
        targets = [t for t in targets if t in live]
        cap = doc.get("concurrency", doc_service.DEFAULT_CONCURRENCY)
        running = [c.get("id") for c in doc.get("cards", [])
                   if c.get("status") == "running"]
        targeted = [t for t in targets if t in running]
        if targeted:
            return _busy(targeted)
        if targets and len(running) >= cap:
            return _busy(running)
        accepted = []
        for target in targets:
            run_id = f"{stored.id}:{uuid.uuid4().hex[:12]}"
            card = next(c for c in doc["cards"]
                        if c.get("id") == target)
            card["status"] = "running"
            card["runId"] = run_id
            accepted.append(run_id)
        doc_service.save_document(stored, doc)
        for run_id in accepted:
            doc_service.run_started(stored.id, run_id)

    for target, run_id in zip(targets, accepted):
        started = time.monotonic()
        try:
            card = _fresh_card(stored, target)
            result, artifact_ids, trace = engine_service.run_card(
                stored, card, current_user.id)
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
            doc_service.run_finished(stored.id, run_id)
        with engine_service.dashboard_lock(stored.id):
            fresh = doc_service.load(stored)
            card = next((c for c in fresh["cards"]
                         if c.get("id") == target), None)
            if card is None or card.get("runId") != run_id:
                # Superseded by a newer run, or removed while we ran: a
                # newer writer owns this card now — never clobber it.
                continue
            card["status"] = status
            card["result"] = result
            card["trace"] = trace
            card["ranAt"] = utcnow().isoformat()
            card["ranSecs"] = round(time.monotonic() - started, 1)
            registry = fresh.setdefault("createdFileIds", [])
            for new_id in artifact_ids:
                if new_id not in registry:
                    registry.append(new_id)
            doc_service.save_document(stored, fresh)
    return jsonify({"ok": True, "accepted": accepted})


def _settle_stream_card(stored_id, card_id, status, result, trace,
                        artifact_ids=()):
    """Persist a streamed run outcome; shared settle shape with run_cards."""
    with engine_service.dashboard_lock(stored_id):
        live = db.session.get(StoredFile, stored_id)
        fresh = doc_service.load(live)
        card = next(c for c in fresh["cards"] if c.get("id") == card_id)
        elapsed = doc_service.run_elapsed(stored_id, card.get("runId"))
        doc_service.run_finished(stored_id, card.get("runId"))
        card["status"] = status
        card["result"] = result
        card["trace"] = trace
        card["ranAt"] = utcnow().isoformat()
        if elapsed is not None:
            card["ranSecs"] = elapsed
        registry = fresh.setdefault("createdFileIds", [])
        for new_id in artifact_ids:
            if new_id not in registry:
                registry.append(new_id)
        doc_service.save_document(live, fresh)


@bp.post("/<int:file_id>/run/stream")
def run_card_stream(file_id):
    """Run one prompt card, streaming agent step frames as SSE.

    Same guards and settle shape as ``run_cards``. Frames are
    ``data: {"t": "step"|"thinking"|"done", ...}``; the client renders
    rows live and reloads the snapshot on the ``done`` frame. If the
    client disconnects, the generator runs to completion so the card
    still settles (the remaining frames are simply not consumed).
    """
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    payload = request.get_json(silent=True) or {}
    card_id = payload.get("card_id")
    if not card_id:
        return jsonify({"ok": False,
                        "error": "Pass card_id."}), 400
    try:
        doc = doc_service.load(stored)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    known = {c.get("id") for c in doc.get("cards", [])}
    if card_id not in known:
        return jsonify({"ok": False,
                        "error": f"Unknown card '{card_id}'."}), 404

    with engine_service.dashboard_lock(stored.id):
        doc = doc_service.load(stored)
        # Same stale-target guard as run_cards: a card deleted between
        # the pre-lock check and this reload 404s instead of raising
        # StopIteration on the bare next() below.
        if card_id not in {c.get("id") for c in doc.get("cards", [])}:
            return jsonify({"ok": False,
                            "error": f"Unknown card '{card_id}'."}), 404
        cap = doc.get("concurrency", doc_service.DEFAULT_CONCURRENCY)
        running = [c.get("id") for c in doc.get("cards", [])
                   if c.get("status") == "running"]
        if card_id in running:
            return _busy([card_id])
        if len(running) >= cap:
            return _busy(running)
        card = next(c for c in doc["cards"] if c.get("id") == card_id)
        run_id = f"{stored.id}:{uuid.uuid4().hex[:12]}"
        card["status"] = "running"
        card["runId"] = run_id
        doc_service.save_document(stored, doc)
        user = db.session.get(User, current_user.id)
        if user is None:
            return jsonify({"ok": False,
                            "error": "Dashboard owner not found."}), 404
        # The generator runs after the view returns, when the request's
        # ORM instances may be detached — re-resolve everything inside.
        stored_id, user_id = stored.id, user.id
        card_snapshot = dict(card)

        def generate():
            live = db.session.get(StoredFile, stored_id)
            live_user = db.session.get(User, user_id)
            # Register the live run before the first frame: reloads (page
            # or /doc) must spare this card while it runs. Registration
            # happens here (not in the view) so a generate() that never
            # starts leaves no stale entry behind.
            doc_service.run_started(stored_id, run_id)
            yield ": connected\n\n"
            workflow_ref = (card_snapshot.get("inputs") or {}).get(
                "workflow") or {}
            if card_snapshot.get("type") == "workflow" or \
                    workflow_ref.get("workflow_id"):
                trace, artifact_ids = [], []
                try:
                    result, artifact_ids, trace = engine_service.run_card(
                        live, card_snapshot, live_user.id)
                    _settle_stream_card(stored_id, card_id, "done",
                                        result, trace, artifact_ids)
                except engine_service.MissingInputError as exc:
                    result = {"error": str(exc)}
                    _settle_stream_card(stored_id, card_id, "error",
                                        result, [])
                except Exception as exc:  # noqa: BLE001
                    db.session.rollback()
                    result = {"error": (str(exc) or "The run failed.")[:500]}
                    _settle_stream_card(stored_id, card_id, "error",
                                        result, [])
                yield "data: " + json.dumps(
                    {"t": "done", "result": result, "trace": trace},
                    default=str) + "\n\n"
                return
            if not card_snapshot.get("tools", True):
                try:
                    refs = (card_snapshot.get("inputs") or {}).get(
                        "attachments") or []
                    files = engine_service.resolve_inputs(
                        live, doc_service.load(live), card_snapshot
                    ) if refs else {}
                    result, _, _ = engine_service._run_prompt_card(
                        live, doc_service.load(live), card_snapshot,
                        live_user, files)
                    _settle_stream_card(stored_id, card_id, "done",
                                        result, [])
                except engine_service.MissingInputError as exc:
                    result = {"error": str(exc)}
                    _settle_stream_card(stored_id, card_id, "error",
                                        result, [])
                except Exception as exc:  # noqa: BLE001
                    db.session.rollback()
                    result = {"error": (str(exc) or "The run failed.")[:500]}
                    _settle_stream_card(stored_id, card_id, "error",
                                        result, [])
                yield "data: " + json.dumps(
                    {"t": "done", "result": result, "trace": []},
                    default=str) + "\n\n"
                return
            it = engine_service.iter_run_events(live, card_snapshot,
                                               live_user)
            try:
                for frame in it:
                    if frame.get("t") == "done":
                        _settle_stream_card(
                            stored_id, card_id, "done",
                            frame.get("result"), frame.get("trace") or [])
                        yield "data: " + json.dumps(frame,
                                                    default=str) + "\n\n"
                        return
                    yield "data: " + json.dumps(frame,
                                                default=str) + "\n\n"
            except GeneratorExit:
                try:
                    for frame in it:
                        if frame.get("t") == "done":
                            _settle_stream_card(
                                stored_id, card_id, "done",
                                frame.get("result"),
                                frame.get("trace") or [])
                            return
                except Exception as exc:  # noqa: BLE001 - drain must settle
                    db.session.rollback()
                    _settle_stream_card(
                        stored_id, card_id, "error",
                        {"error": (str(exc) or "The run failed.")[:500]},
                        [])
            except engine_service.MissingInputError as exc:
                result = {"error": str(exc)}
                _settle_stream_card(stored_id, card_id, "error", result, [])
                yield "data: " + json.dumps(
                    {"t": "done", "result": result, "trace": []}) + "\n\n"
            except Exception as exc:  # noqa: BLE001 - errors surface on card
                db.session.rollback()
                result = {"error": (str(exc) or "The run failed.")[:500]}
                _settle_stream_card(stored_id, card_id, "error", result, [])
                yield "data: " + json.dumps(
                    {"t": "done", "result": result, "trace": []}) + "\n\n"

        return Response(stream_with_context(generate()),
                        mimetype="text/event-stream")


def _run_card_payload(stored, run_id):
    try:
        doc = doc_service.load(stored)
    except ValueError:
        return None, None
    card = next((c for c in doc.get("cards", [])
                 if c.get("runId") == run_id), None)
    return doc, card


@bp.get("/runs/<run_id>/status")
def run_status(run_id):
    """Poll one card run — ``{status, result?, error?}``, mirroring the
    automation run-status shape (plus run/card/dashboard ids)."""
    try:
        file_part, _ = run_id.split(":", 1)
        file_id = int(file_part)
    except (ValueError, AttributeError):
        abort(404)
    stored = _get_dashboard(file_id)
    _doc, card = _run_card_payload(stored, run_id)
    if card is None:
        abort(404)
    error = None
    if card.get("status") in ("error", "interrupted"):
        result = card.get("result") or {}
        error = (result.get("error") if isinstance(result, dict)
                 else str(result))
    return jsonify({"run_id": run_id, "card_id": card.get("id"),
                    "dashboard_id": stored.id,
                    "status": card.get("status"),
                    "result": card.get("result"), "error": error,
                    "ranAt": card.get("ranAt")})


@bp.get("/<int:file_id>/status")
def dashboard_status(file_id):
    """Aggregate card states for the whole dashboard (polled by View)."""
    stored = _get_dashboard(file_id)
    try:
        doc = doc_service.load(stored)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    cards = {c.get("id"): {"status": c.get("status"),
                           "ranAt": c.get("ranAt"),
                           "ranSecs": c.get("ranSecs"),
                           "runId": c.get("runId")}
             for c in doc.get("cards", [])}
    running = [cid for cid, state in cards.items()
               if state["status"] == "running"]
    return jsonify({"ok": True, "dashboard_id": stored.id,
                    "running": running, "cards": cards})


# ---------------------------------------------------------------------------
# Cleanup: artifacts and dashboard delete
# ---------------------------------------------------------------------------

@bp.post("/<int:file_id>/artifacts/clear")
def artifacts_clear(file_id):
    """Trash run-created artifacts; keep the dashboard and its attachments."""
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    try:
        result = doc_service.clear_artifacts(stored)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@bp.get("/<int:file_id>/delete-info")
def delete_info(file_id):
    """Inventory for the delete confirm modal: what removal deletes
    (dashboard + owned uploads + artifacts) and what it keeps (linked
    originals)."""
    stored = _get_dashboard(file_id)
    try:
        info = doc_service.delete_info(stored)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **info})


@bp.post("/<int:file_id>/delete")
def delete_dashboard(file_id):
    """Remove the dashboard plus owned uploads and run-created artifacts
    after explicit confirmation. Linked originals are never touched."""
    stored = _get_dashboard(file_id)
    _guard_writable(stored)
    try:
        result = doc_service.delete_dashboard(stored)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})
