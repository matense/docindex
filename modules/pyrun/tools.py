"""AI tools contributed by the pyrun module (namespaced pyrun.*).

The agent writes a small Python program; it runs in the sealed
``evaluate()`` sandbox and plain values come back (plus optional named
text artifacts the agent then persists with ``pyrun.save`` through the
same validated save path as every other upload flow). Agent-only in
v1: no routes, no nav.
"""

import codecs
import json
import mimetypes
import os
import uuid
from io import BytesIO

from flask import current_app
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import StoredFile, User
from app.services import ai_usage_service, file_service
from app.services.agent_service import register_tool

from .evaluate import evaluate

_CODE_MAX_CHARS = 20_000
_FILES_MAX_COUNT = 5
_INPUTS_MAX_CHARS = 200_000
_SMALL_FILE_MAX_CHARS = 200_000
_CHUNK_BYTES = 65536
# Hard backstops for the streamed large-file path: a pathological file
# fails closed (ValueError -> safe tool error) instead of blowing RAM.
_LARGE_FILE_MAX_BYTES = 16 * 1024 * 1024
_LARGE_FILE_MAX_CHUNKS = 512

# Pending artifacts: key (uuid hex) -> {name, content, size}. Bounded so
# abandoned compute outputs cannot grow RAM without limit: at most 50
# entries / 10MB total, oldest first.
_PENDING = {}
_PENDING_MAX_ENTRIES = 50
_PENDING_MAX_BYTES = 10 * 1024 * 1024


def _schema(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required)}}}


def _err(message, hint):
    return {"ok": False, "error": message, "retry_hint": hint}


def _uid(user):
    return user.id if hasattr(user, "id") else user


def _as_user(user):
    if hasattr(user, "id"):
        return user
    try:
        return db.session.get(User, int(user))
    except (TypeError, ValueError):
        return None


def _record(user_id, code, value=None):
    """One AI-usage row per tool call (audit trail, never raises)."""
    try:
        text = code if isinstance(code, str) else ""
        try:
            completion = json.dumps(value, default=str) or ""
        except (TypeError, ValueError):
            completion = ""
        ai_usage_service.record_usage(
            user_id,
            {"prompt_tokens": max(1, len(text) // 4),
             "completion_tokens": max(0, len(completion) // 4),
             "estimated": True},
            model="pyrun", kind="pyrun")
    except Exception:  # noqa: BLE001 - tracking never breaks the answer
        pass


def _read_chunks(stored):
    """Text chunks for the evaluator: one read for small files, streamed
    line-aligned chunks for large ones (each chunk ticks the wall clock).

    Large-file contract (shared with ``evaluate._bi_rows``): the byte
    stream is decoded with an incremental UTF-8 decoder (multibyte chars
    never split) and split on newlines, so every chunk ends at a line
    boundary. The first line of the file is the header and lives in the
    first chunk only; later chunks are pure data lines (no repeated
    header). ``"".join(chunks)`` still reproduces the file text exactly.
    Totals are capped; oversized files raise (the caller maps any read
    failure to a safe retryable error).
    """
    if (stored.size or 0) <= _SMALL_FILE_MAX_CHARS:
        return [file_service.read_text_content(
            stored, max_chars=_SMALL_FILE_MAX_CHARS)]
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    chunks = []
    pending = []  # complete lines for the in-progress chunk
    pending_chars = 0
    buf = ""  # decoded text not yet split into lines
    total_bytes = 0
    ended_newline = True
    with open(file_service.file_path(stored), "rb") as fh:
        while True:
            blob = fh.read(_CHUNK_BYTES)
            if blob:
                total_bytes += len(blob)
                if total_bytes > _LARGE_FILE_MAX_BYTES:
                    raise ValueError(
                        "File is too large to process in the sandbox.")
                buf += decoder.decode(blob)
                final = False
            else:
                buf += decoder.decode(b"", final=True)
                final = True
            lines = buf.split("\n")
            if final:
                if lines and lines[-1] == "":
                    lines.pop()  # newline terminator, not a line
                    ended_newline = True
                else:
                    ended_newline = False
                complete = lines
                buf = ""
            else:
                complete, buf = lines[:-1], lines[-1]
            for line in complete:
                pending.append(line)
                pending_chars += len(line) + 1  # +1 for the split "\n"
                if pending_chars >= _CHUNK_BYTES:
                    chunks.append("\n".join(pending) + "\n")
                    if len(chunks) > _LARGE_FILE_MAX_CHUNKS:
                        raise ValueError(
                            "File has too many rows to process.")
                    pending = []
                    pending_chars = 0
            if final:
                break
    if pending:
        text = "\n".join(pending)
        chunks.append(text + ("\n" if ended_newline else ""))
    return chunks or [""]


def _resolve_input_files(user, entries):
    """(name -> chunks, None), or (None, error response).

    Every entry is ownership-checked exactly like the file tools: an
    unknown, foreign-owned or trashed id rejects the whole call — files
    are never silently skipped.
    """
    if not isinstance(entries, list):
        return None, _err(
            "Pass 'files' as a list like [{file_id: 12, as: 'f'}].",
            "Pass 'files' as a list of {file_id, as} objects and retry.")
    if len(entries) > _FILES_MAX_COUNT:
        return None, _err(
            f"Too many files ({len(entries)} > {_FILES_MAX_COUNT}).",
            "Attach at most 5 files per call — process slices and "
            "combine partial results across calls, then retry.")
    files_map = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None, _err(
                "Each 'files' entry must be {file_id, as}.",
                "Pass 'files' as a list of {file_id, as} objects "
                "and retry.")
        try:
            file_id = int(entry.get("file_id"))
        except (TypeError, ValueError):
            return None, _err(
                f"Invalid file id {entry.get('file_id')!r}.",
                "Use an integer file_id you own that is not trashed "
                "and retry.")
        try:
            stored = (StoredFile.query
                      .filter_by(id=file_id, user_id=_uid(user))
                      .filter(StoredFile.deleted_at.is_(None))
                      .first())
        except Exception:  # noqa: BLE001 - DB failures are retryable
            return None, _err(
                f"Could not read file {file_id}.",
                "Pick another file and retry.")
        if stored is None:
            return None, _err(
                f"File {file_id} not found.",
                "Use a file_id you own that is not trashed — list "
                "your files to pick one — and retry.")
        as_name = entry.get("as", f"f{file_id}")
        if not isinstance(as_name, str) or not as_name:
            return None, _err(
                "Each 'files' entry needs a non-empty 'as' name.",
                "Name each file with 'as' (e.g. 'f') and retry.")
        if as_name in files_map:
            return None, _err(
                f"Duplicate file name {as_name!r}.",
                "Give each file a distinct 'as' name and retry.")
        try:
            files_map[as_name] = _read_chunks(stored)
        except Exception:  # noqa: BLE001 - any read failure is retryable
            return None, _err(
                f"Could not read file {file_id}.",
                "Pick another file and retry.")
    return files_map, None


def _check_artifact_name(name):
    """Error string, or None when the name is a safe bare file name."""
    if not isinstance(name, str) or not name.strip():
        return "Artifact name must be a non-empty file name."
    if len(name) > 255:
        return "Artifact name is too long (max 255 characters)."
    if name != name.strip() or name in (".", ".."):
        return f"Artifact name {name!r} is not allowed."
    if "/" in name or "\\" in name or "\x00" in name:
        return (f"Artifact name {name!r} must be a bare file name "
                "without folders.")
    if os.path.basename(name) != name:
        return (f"Artifact name {name!r} must be a bare file name "
                "without folders.")
    return None


def _pending_bytes():
    return sum(entry["size"] for entry in _PENDING.values())


def _register_artifact(user_id, name, content):
    key = uuid.uuid4().hex
    _PENDING[key] = {"name": name, "content": content,
                     "size": len(content.encode("utf-8")),
                     "user_id": user_id}
    while (len(_PENDING) > _PENDING_MAX_ENTRIES
           or _pending_bytes() > _PENDING_MAX_BYTES):
        _PENDING.pop(next(iter(_PENDING)))
    return key


def _tool_compute(user, args, drive=None):
    user = _as_user(user)
    if user is None:
        return _err("Unknown user.",
                    "Authenticate and retry.")
    if not isinstance(args, dict):
        _record(None, "")
        return _err("Pass an object with 'code' (a Python program).",
                    "Pass {'code': '...'} and retry.")
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        _record(user.id, code if isinstance(code, str) else "")
        return _err(
            "Pass 'code' as a non-empty Python string "
            f"(max {_CODE_MAX_CHARS} chars).",
            "Pass the program as 'code' and retry.")
    if len(code) > _CODE_MAX_CHARS:
        _record(user.id, code)
        return _err(
            f"Code is too large ({len(code)} > {_CODE_MAX_CHARS} chars).",
            "Split the program into smaller steps and retry.")
    inputs = args.get("inputs", {})
    if not isinstance(inputs, dict):
        _record(user.id, code)
        return _err(
            "Pass 'inputs' as an object of JSON values.",
            "Pass 'inputs' as {name: value} with plain JSON values "
            "and retry.")
    try:
        serialized_inputs = json.dumps(inputs, allow_nan=False)
    except (TypeError, ValueError):
        _record(user.id, code)
        return _err(
            "Pass 'inputs' as an object of JSON values.",
            "Pass 'inputs' as {name: value} with plain JSON values "
            "and retry.")
    if len(serialized_inputs) > _INPUTS_MAX_CHARS:
        _record(user.id, code)
        return _err(
            f"Inputs are too large ({len(serialized_inputs)} > "
            f"{_INPUTS_MAX_CHARS} chars).",
            "Pass smaller inputs — process slices and combine partial "
            "results across calls, then retry.")
    outputs = args.get("outputs", {})
    if not isinstance(outputs, dict):
        _record(user.id, code)
        return _err(
            "Pass 'outputs' as {file name: variable}.",
            "Name outputs as {file name: variable} with bare file "
            "names and retry.")
    for name, var in outputs.items():
        problem = _check_artifact_name(name)
        if problem is not None:
            _record(user.id, code)
            return _err(problem,
                        "Name outputs as {file name: variable} with bare "
                        "file names (no folders) and retry.")
        if not isinstance(var, str) or not var.isidentifier():
            _record(user.id, code)
            return _err(
                f"Output {name!r} must name a variable assigned by "
                "the code.",
                "Map each output file to a variable the code assigns "
                "and retry.")
    files_map, error = _resolve_input_files(user, args.get("files", []))
    if error is not None:
        _record(user.id, code)
        return error
    try:
        outcome = evaluate(code, variables=inputs, files=files_map)
    except Exception:  # noqa: BLE001 - the tool boundary never 500s
        _record(user.id, code)
        return _err(
            "Evaluation failed unexpectedly.",
            "Simplify the program (smaller inputs, fewer steps) "
            "and retry.")
    if not outcome.get("ok"):
        _record(user.id, code)
        return {"ok": False,
                "error": outcome.get("error") or "Evaluation failed.",
                "retry_hint": "Revise the code for the reported error "
                              "and retry."}
    artifacts = []
    if outputs:
        probe = ("{" + ", ".join(f"{json.dumps(name)}: ({var})"
                                 for name, var in outputs.items()) + "}")
        try:
            collected = evaluate(code + "\n" + probe, variables=inputs,
                                 files=files_map)
        except Exception:  # noqa: BLE001 - the tool boundary never 500s
            _record(user.id, code, outcome.get("value"))
            return {"ok": False,
                    "error": "Evaluation failed unexpectedly.",
                    "retry_hint": "The 'outputs' names must match "
                                  "variables the code assigns — fix the "
                                  "code and retry."}
        if not collected.get("ok"):
            _record(user.id, code, outcome.get("value"))
            return {"ok": False,
                    "error": collected.get("error") or "Evaluation failed.",
                    "retry_hint": "The 'outputs' names must match "
                                  "variables the code assigns — fix the "
                                  "code and retry."}
        values = collected.get("value") or {}
        for name in outputs:
            text = values.get(name)
            if not isinstance(text, str):
                _record(user.id, code, outcome.get("value"))
                return _err(
                    f"Output {name!r} is not text.",
                    "Assign a string to each output variable and retry.")
            artifacts.append(
                {"key": _register_artifact(user.id, name, text),
                 "name": name,
                 "size": len(text.encode("utf-8"))})
    _record(user.id, code, outcome.get("value"))
    return {"ok": True, "value": outcome.get("value"),
            "artifacts": artifacts}


def _trigger_indexing(file_id):
    """Index a saved artifact now (tests) or queue it, like /upload."""
    from app.services import indexing_service
    if current_app.config.get("INDEX_ASYNC", True):
        indexing_service.enqueue_index([file_id])
        indexing_service.ensure_workers(1)
    else:
        indexing_service.index_file(file_id)


def _tool_save(user, args, drive=None):
    user = _as_user(user)
    if user is None:
        return _err("Unknown user.",
                    "Authenticate and retry.")
    if not isinstance(args, dict):
        return _err("Pass an object with 'key' and 'drive_id'.",
                    "Save a pyrun artifact with {key, drive_id} "
                    "and retry.")
    key = args.get("key")
    entry = _PENDING.get(key) if isinstance(key, str) else None
    if entry is None or entry.get("user_id") != user.id:
        _record(user.id, "")
        return _err(
            "Unknown artifact key — keys are single-use and expire.",
            "Re-run pyrun.compute with 'outputs' to get a fresh key "
            "and retry.")
    name = args.get("name") or entry["name"]
    problem = _check_artifact_name(name)
    if problem is not None:
        _record(user.id, "")
        return _err(problem,
                    "Use a bare file name without folders and retry.")
    dest_drive, folder, dest_error = file_service._resolve_dest(
        user, args.get("drive_id"), args.get("folder_id"))
    if dest_error:
        _record(user.id, "")
        return _err(dest_error,
                    "Pick a writable drive you own (drive_id) with an "
                    "optional folder_id inside it, and retry.")
    if not file_service.allowed_file(name):
        _record(user.id, "")
        return _err(f"File type not allowed for {name!r}.",
                    "Save as a supported document/data type (e.g. .csv, "
                    ".txt, .md, .json) and retry.")
    content = entry["content"].encode("utf-8")
    upload = FileStorage(
        stream=BytesIO(content), filename=name,
        content_type=mimetypes.guess_type(name)[0]
        or "application/octet-stream")
    try:
        stored = file_service.save_upload(upload, user, folder,
                                          drive=dest_drive)
    except ValueError as exc:
        _record(user.id, "")
        return _err(str(exc) or "Could not save the file.",
                    "Fix the reported problem and retry.")
    except Exception:  # noqa: BLE001 - IO/SQLAlchemy must not 500
        _record(user.id, "")
        return _err("Could not save the file.",
                    "Fix the reported problem and retry.")
    stored.drive_id = dest_drive.id if dest_drive else None
    db.session.commit()
    _trigger_indexing(stored.id)
    _PENDING.pop(key, None)
    _record(user.id, "", entry["content"])
    return {"ok": True, "file_id": stored.id, "name": stored.name,
            "drive_id": stored.drive_id, "size": stored.size}


register_tool(
    "compute",
    _schema("pyrun.compute",
            "Run a small, bounded Python program in a sealed sandbox and "
            "get a JSON value back. The last top-level expression is the "
            "returned value. Use rows('name') (list of str->str dicts) "
            "for attached files, plus parse_json/parse_csv and the "
            "whitelisted builtins (len range sum min max sorted abs round "
            "enumerate zip str int float bool list dict set tuple). Files "
            "are also seeded as TEXT tables t0, t1, ... (insertion order) "
            "in a throwaway in-memory DB: db.query(sql[, params]) runs one "
            "SELECT/WITH statement (list of dicts), db.exec(sql[, params]) "
            "runs one write/DDL statement ({rows_affected}). No "
            "imports, no filesystem, no network. 'outputs' maps an "
            "artifact file name (bare name, no folders) to a variable "
            "holding its text — the artifact is held pending and "
            "persisted only via pyrun.save. Prefer this for mechanical "
            "data crunching (aggregates, filters, reshapes); one call "
            "counts as one agent step.",
            {"code": {"type": "string",
                       "description": "Python program (max 20000 chars)."},
             "inputs": {"type": "object",
                        "description": "JSON values available as "
                                       "variables."},
             "files": {"type": "array",
                       "items": {"type": "object"},
                       "description": "Files to expose: [{file_id, "
                                      "as}] — 'as' names the rows() "
                                      "table (default f<file_id>)."},
             "outputs": {"type": "object",
                         "description": "{artifact file name: variable} "
                                        "— text results held pending "
                                        "for pyrun.save."}},
            required=["code"]),
    _tool_compute, label="Ran Python sandbox", module="pyrun")

register_tool(
    "save",
    _schema("pyrun.save",
            "Persist a pending pyrun.compute artifact into a validated "
            "drive destination the user owns (same ownership path as "
            "every other upload). Artifact keys are single-use: a "
            "successful save consumes the key.",
            {"key": {"type": "string",
                      "description": "Pending artifact key from "
                                     "pyrun.compute."},
             "drive_id": {"type": "integer",
                          "description": "Target drive (omit for root)."},
             "folder_id": {"type": "integer",
                           "description": "Folder inside the drive."},
             "name": {"type": "string",
                      "description": "Bare file name override (no "
                                     "folders)."}},
            required=["key"]),
    _tool_save, label="Saved sandbox artifact", module="pyrun")
