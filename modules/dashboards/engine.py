"""Dashboard execution engine: per-card runs over a sequential queue.

A card run snapshots its result back into the dashboard document through
``doc.save_document`` (write-through for results, never a version per
refresh). Prompt cards run the AI agent with the full tool loop (tools on)
or a single LLM call over pre-injected, truncated file context (tools off);
workflow cards run the referenced automation workflow via
``runtime.start_run`` and, when the card carries a prompt, pipe the output
through one AI format/summarize pass.

Runs execute inline (synchronously) inside the ``POST /run`` request, the
same way the test suite runs automation/AI tasks with
``AUTOMATION_ASYNC=False``/``AI_TASKS_ASYNC=False``. The per-dashboard queue
is the card ``status`` in the doc plus a per-dashboard lock: the run route
marks targets ``running`` under the lock, executes them sequentially, and
settles each card to ``done``/``error``. Refreshing a running card is
rejected (409) instead of queued; cards stuck in ``running`` on load are
marked ``interrupted`` by ``doc.recover_interrupted``.

The AI entry points are module-level seams (``run_agent_answer``,
``run_agent_collected``, ``complete_once``, ``start_workflow_run``) so tests can stub them; usage is
recorded through the existing AI usage events for every model call.
"""

import importlib
import json
import re
import threading
import time

from app.extensions import db
from app.models import StoredFile
from app.services import ai_usage_service, file_service, indexing_service

from . import doc as doc_service

# Tools-off context budget: attached files are inlined up to this many
# characters total; anything beyond is cut and flagged on the card.
TOOLS_OFF_MAX_CHARS = 20_000

# Binary workbook extensions: a raw text read of these is zip garbage, so
# build_context uses the indexed extracted text instead.
_SPREADSHEET_EXTS = {"xlsx", "xlsm", "xls"}

# How long a workflow-backed card waits for its automation run (the test
# suite runs them inline, so this only binds in async production mode).
RUN_POLL_INTERVAL_S = 1.0
RUN_POLL_TIMEOUT_S = 180

# Per-type output contracts (Task 10): the run prompt tells the model the
# exact shape each card type needs, and bans file references in answers
# (agents used to echo "(Ficheiros usados: ... (file://...))" into results).
# KPI/chart answers are parsed back into structured snapshots; anything
# unparseable keeps the old {"text"} behavior (never an error).
_NO_FILE_REFS = ("Never include file names, file IDs or file:// references "
                 "in the answer.")
_OUTPUT_CONTRACTS = {
    "kpi": ("--- Output contract (KPI card) ---\n"
            "Reply with ONLY a JSON object, no prose. Shape: "
            '{"value": "...", "label": "..."} — "value" is the single '
            'number/text shown big (e.g. "177,8 kWh"), "label" is what it '
            'refers to (e.g. "jun/2026"). Optional "delta" (e.g. "+5%"). '
            "If you must explain your reasoning, write it BEFORE the JSON "
            "and keep the JSON as one fenced ```json block. "
            + _NO_FILE_REFS),
    "chart": ("--- Output contract (chart card) ---\n"
              "Reply with ONLY a JSON object, no prose. Shape: "
              '{"chart": {"kind": "bar|line|pie|doughnut|horizontalBar|area", '
              '"labels": [...up to 50...], '
              '"datasets": [{"label": "...", "data": [numbers...]}]}}. '
              "Kinds: bar, line, pie, doughnut, horizontalBar (bars run "
              "left-to-right), area (filled line). "
              "If you must explain your reasoning, write it BEFORE the JSON "
              "and keep the JSON as one fenced ```json block. "
              + _NO_FILE_REFS),
    "list": ("--- Output contract (list card) ---\n"
             "Reply with one item per line, no numbering or bullets. "
             + _NO_FILE_REFS),
    "table": ("--- Output contract (table card) ---\n"
              "Reply as plain rows, one per line, columns separated by "
              "' | ', first line the header. " + _NO_FILE_REFS),
}

# Chart kinds the view renderer knows how to draw (Chart.js v4: doughnut
# is native; horizontalBar renders as bar with indexAxis "y"; area renders
# as a filled line). Prompt and script cards coerce through the same
# _parse_chart_result, so one allowlist covers both paths.
_CHART_KINDS = ("bar", "line", "pie", "doughnut", "horizontalBar", "area")

# Notebook chart-cell caps, mirrored for dashboard chart snapshots.
_CHART_MAX_LABELS = 50
_CHART_MAX_DATASETS = 500

# Table snapshot caps (mirror the notebooks table shape).
_TABLE_MAX_COLS = 50
_TABLE_MAX_ROWS = 500

# Run-trace caps: a chatty model must not blow up the card document.
_TRACE_MAX_ENTRIES = 128
_TRACE_DETAIL_CHARS = 500
_TRACE_THINKING_CHARS = 2000


def _output_contract(card_type):
    """The prompt suffix for a card type (empty for markdown/workflow)."""
    return _OUTPUT_CONTRACTS.get(card_type or "", "")


def _balanced_blocks(answer):
    """Top-level {...} spans, innermost-nested ignored.

    String-aware (double quotes + backslash escapes only — JSON strings
    cannot use single quotes, so prose apostrophes never break the
    counting). Yields the raw span text, outermost spans only.
    """
    spans, depth, start = [], 0, -1
    in_string, escaped = False, False
    for i, ch in enumerate(answer):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(answer[start:i + 1])
                    start = -1
    return spans


def _extract_json_block(answer):
    """First JSON object in an answer: fenced block, whole text, else the
    last balanced {...} span (models often write prose first and append
    the JSON at the end)."""
    if not isinstance(answer, str):
        return None
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.S)
    candidates = [match.group(1)] if match else []
    candidates.append(answer.strip())
    candidates.extend(reversed(_balanced_blocks(answer)))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _parse_kpi_result(answer):
    """KPI answers become {"kpi": {...}}; prose keeps {"text"}."""
    data = _extract_json_block(answer)
    if data is not None:
        payload = data.get("kpi") if isinstance(data.get("kpi"),
                                                dict) else data
        if (isinstance(payload, dict)
                and payload.get("value") not in (None, "")):
            kpi = {"value": payload["value"]}
            for key in ("label", "delta"):
                if payload.get(key) not in (None, ""):
                    kpi[key] = payload[key]
            return {"kpi": kpi}
    return {"text": answer}


def _coerce_number(value):
    """A numeric cell: ints stay ints, floats stay floats, else 0."""
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if number != number or number in (float("inf"), float("-inf")):
        return 0
    return int(number) if number.is_integer() else number


def _parse_chart_result(answer):
    """Chart answers become {"chart": {...}} (notebook validation/caps)."""
    data = _extract_json_block(answer)
    spec = None
    if isinstance(data, dict):
        inner = data.get("chart")
        spec = inner if isinstance(inner, dict) else data
    if not isinstance(spec, dict):
        return {"text": answer}
    kind = spec.get("kind")
    kind = kind if kind in _CHART_KINDS else "bar"
    raw_labels = spec.get("labels")
    labels = ([str(label) for label in raw_labels]
              if isinstance(raw_labels, list) else [])
    labels = labels[:_CHART_MAX_LABELS]
    raw_datasets = spec.get("datasets")
    raw_datasets = raw_datasets if isinstance(raw_datasets, list) else []
    if not labels or not raw_datasets:
        return {"text": answer}
    datasets = []
    for entry in raw_datasets[:_CHART_MAX_DATASETS]:
        if not isinstance(entry, dict):
            continue
        raw_data = entry.get("data")
        raw_data = raw_data if isinstance(raw_data, list) else []
        values = [_coerce_number(v) for v in raw_data]
        values = values[:_CHART_MAX_LABELS]
        if not values:
            continue
        datasets.append({"label": str(entry.get("label") or ""),
                         "data": values})
    if not datasets:
        return {"text": answer}
    chart = {"kind": kind, "labels": labels, "datasets": datasets}
    if spec.get("title") not in (None, ""):
        chart["title"] = str(spec.get("title"))[:200]
    return {"chart": chart}


def _parse_table_result(answer):
    """Pipe-row answers become {"table": {"headers", "rows"}}.

    The table output contract asks for one `a | b | c` row per line with
    the header first. Ragged rows are padded/cut to the header width;
    anything without a usable header plus at least one data row keeps
    the old {"text"} behavior.
    """
    if not isinstance(answer, str):
        return {"text": answer}
    rows = []
    for line in answer.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not any(cells):
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue  # markdown separator row, not data
        rows.append(cells)
    if len(rows) < 2 or not any(rows[0]):
        return {"text": answer}
    headers = [str(h) for h in rows[0][: _TABLE_MAX_COLS]]
    width = len(headers)
    data = []
    for row in rows[1:][: _TABLE_MAX_ROWS]:
        cells = [str(c) for c in row[:width]]
        cells += [""] * (width - len(cells))
        if any(cells):
            data.append(cells)
    if not data:
        return {"text": answer}
    return {"table": {"headers": headers, "rows": data}}


def _parse_prompt_result(card_type, answer):
    """Structured snapshot for kpi/chart/table answers; {"text"} otherwise."""
    if card_type == "kpi":
        return _parse_kpi_result(answer)
    if card_type == "chart":
        return _parse_chart_result(answer)
    if card_type == "table":
        return _parse_table_result(answer)
    return {"text": answer}


def _parse_script_result(card_type, value):
    """Coerce a pyrun value through the card's prompt-result contract.

    Non-string values serialize first (JSON for dict/list, str otherwise),
    then the existing per-type parsing applies — except table cards, where
    structured values ({"headers", "rows"} or a bare row list, the natural
    db.query shapes) coerce directly into the table snapshot with the same
    caps as the pipe path. None maps to "" (never a literal "None"
    snapshot).
    """
    if value is None:
        value = ""
    if card_type == "table" and isinstance(value, (dict, list)):
        table = _parse_script_table(value)
        if table is not None:
            return table
    if not isinstance(value, str):
        value = (json.dumps(value) if isinstance(value, (dict, list))
                 else str(value))
    return _parse_prompt_result(card_type, value)


def _parse_script_table(value):
    """Table snapshot from a script value, or None (→ text fallback).

    Shapes: {"headers": [...], "rows": [[...]|{...}, ...]} or a bare list
    of row dicts (headers = key union, first-seen) / row lists (first
    row = header, mirroring the pipe convention). Cells stringify
    (nested objects as JSON), ragged rows pad/cut to the header width,
    fully-empty rows drop. Caps mirror the pipe path: 50 columns,
    500 rows; over-cap snapshots keep the first 500 with total_rows and
    truncated_rows set for the pager.
    """
    headers, raw = None, None
    if isinstance(value, dict):
        if not isinstance(value.get("headers"), list) or not isinstance(
                value.get("rows"), list):
            return None
        headers, raw = value["headers"], value["rows"]
    elif isinstance(value, list):
        if not value:
            return None
        if all(isinstance(r, dict) for r in value):
            union = []
            for r in value:
                for k in r:
                    if k not in union:
                        union.append(k)
            headers, raw = union, value
        elif all(isinstance(r, list) for r in value):
            headers, raw = value[0], value[1:]
        else:
            return None
    else:
        return None
    headers = list(headers)[:_TABLE_MAX_COLS]
    width = len(headers)
    if not width:
        return None
    str_headers = [str(h) for h in headers]

    def _cell(v):
        if isinstance(v, (dict, list)):
            return json.dumps(v)
        return str(v)

    data = []
    for row in raw:
        cells = ([_cell(c) for c in row[:width]]
                 if isinstance(row, list)
                 else [_cell(row.get(h, "")) for h in headers])
        cells += [""] * (width - len(cells))
        if any(cells):
            data.append(cells)
    if not data:
        return None
    total = len(data)
    return {"table": {"headers": str_headers,
                      "rows": data[:_TABLE_MAX_ROWS]},
            "total_rows": total,
            "truncated_rows": total > _TABLE_MAX_ROWS}


class MissingInputError(ValueError):
    """A card input is gone: trashed/moved/deleted file or workflow."""


_locks_guard = threading.Lock()
# Per-dashboard run locks, keyed by dashboard file id.
#
# DEPLOYMENT ASSUMPTION: single-worker gunicorn (Dockerfile runs
# ``--workers 1 --threads 8``), so one process holds all threads and this
# dict + the lock held across the whole check-and-mark-and-run section in
# ``routes.run_cards`` is a correct mutual-exclusion guard. Moving to
# ``--workers > 1`` would make the cap per-process only — promote this to
# a DB/file lock first (e.g. SELECT ... FOR UPDATE on the dashboard row
# or a lock file under the file store).
_dashboard_locks = {}


def dashboard_lock(file_id):
    """Per-dashboard lock serializing concurrent ``POST /run`` requests."""
    with _locks_guard:
        lock = _dashboard_locks.get(file_id)
        if lock is None:
            lock = threading.Lock()
            _dashboard_locks[file_id] = lock
        return lock


def _automation():
    """The loaded automation module (runtime + models), whatever name the
    loader used — ``docindex_module_automation`` in the app/test harness,
    ``modules.automation`` as a plain namespace-package fallback."""
    for name in ("docindex_module_automation", "modules.automation"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise RuntimeError("Automation module is not available.")


def _automation_models():
    return importlib.import_module(f"{_automation().__name__}.models")


def _automation_runtime():
    return importlib.import_module(f"{_automation().__name__}.runtime")


# ---------------------------------------------------------------------------
# AI seams (stubbed in tests; production bodies below)
# ---------------------------------------------------------------------------

def run_agent_answer(user, prompt, drive=None, max_steps=None):
    """Full agent run with the tool loop; returns (answer_text, [usages])."""
    answer, usages, _trace = run_agent_collected(
        user, prompt, drive=drive, max_steps=max_steps)
    return answer, usages


def run_agent_collected(user, prompt, drive=None, max_steps=None):
    """Full agent run; returns (answer_text, [usages], trace)."""
    from app.services import agent_service
    answer, usages, trace = "", [], []
    for frame in _iter_agent_frames(agent_service, user, prompt, drive,
                                   max_steps):
        if frame["t"] == "answer":
            answer = frame["text"]
        elif frame["t"] == "usage":
            usages.append(frame["usage"])
        elif frame["t"] in ("step", "thinking"):
            trace.append({k: frame[k] for k in ("index", "label", "detail")
                          if k in frame})
    return answer, usages, trace


def _iter_agent_frames(agent_service, user, prompt, drive, max_steps):
    """Yield step/thinking/answer/usage frame dicts from the agent loop."""
    trace_len, thinking_chars = 0, 0
    for kind, payload in agent_service.run_agent_events(
            user, [{"role": "user", "content": prompt}], drive=drive,
            max_steps=max_steps):
        if kind == "answer":
            yield {"t": "answer", "text": payload
                   if isinstance(payload, str) else str(payload or "")}
        elif kind == "usage" and isinstance(payload, dict):
            yield {"t": "usage", "usage": payload}
        elif kind in ("tool_start", "step") and isinstance(payload, dict):
            if trace_len >= _TRACE_MAX_ENTRIES:
                continue
            detail = payload.get("detail", "")
            if kind == "tool_start" and not detail:
                detail = payload.get("name", "")
            trace_len += 1
            yield {"t": "step", "index": payload.get("index"),
                   "label": str(payload.get("label") or kind),
                   "detail": str(detail or "")[:_TRACE_DETAIL_CHARS]}
        elif kind == "thinking" and isinstance(payload, str) and payload:
            room = _TRACE_THINKING_CHARS - thinking_chars
            if room > 0:
                chunk = payload[:room]
                thinking_chars += len(chunk)
                yield {"t": "thinking", "label": "thinking",
                       "detail": chunk}


def iter_run_events(stored, card, user):
    """Stream a prompt-card run as frame dicts, ending with a done frame.

    Frames: {"t": "step", index, label, detail}, {"t": "thinking",
    detail}, then {"t": "done", "result": ..., "trace": ...}. Non-string
    thinking payloads are skipped; caps mirror the stored trace.
    Usage is recorded when a user with an id is given (tests pass None).
    """
    prompt = (card.get("prompt") or "").strip()
    refs = (card.get("inputs") or {}).get("attachments") or []
    if not prompt and not refs:
        raise ValueError("Card has no prompt and no inputs — nothing to run.")
    lines = [prompt] if prompt else ["Summarize the attached files."]
    if refs:
        if stored is None:
            raise ValueError("Card has attachments but no dashboard — "
                             "nothing to run.")
        files = resolve_inputs(stored, doc_service.load(stored), card)
        lines += ["", "Attached files (read them with the file tools):"]
        for ref in refs:
            target = files[ref]
            lines.append(f"- {target.name} (file_id {target.id})")
    contract = _output_contract(card.get("type"))
    if contract:
        lines += ["", contract]
    drive = stored.drive if stored is not None else None
    from app.services import agent_service
    answer, usages, trace = "", [], []
    thinking, thinking_chars = [], 0
    for frame in _iter_agent_frames(agent_service, user, "\n".join(lines),
                                   [drive] if drive is not None else None,
                                   card.get("maxSteps")):
        if frame["t"] == "answer":
            answer = frame["text"]
        elif frame["t"] == "usage":
            usages.append(frame["usage"])
        elif frame["t"] == "step":
            trace.append({k: frame[k] for k in ("index", "label", "detail")})
            yield dict(frame, card_id=card.get("id"))
        elif frame["t"] == "thinking":
            thinking.append(frame["detail"])
            thinking_chars += len(frame["detail"])
            yield dict(frame, card_id=card.get("id"))
    if thinking_chars:
        trace.append({"index": None, "label": "thinking",
                      "detail": "".join(thinking)})
    if user is not None and hasattr(user, "id"):
        _record_usage(user.id, usages)
    yield {"t": "done", "card_id": card.get("id"),
           "result": _parse_prompt_result(card.get("type"), answer),
           "trace": trace}


def complete_once(user, messages):
    """Single LLM call; returns (text, usage dict)."""
    from app.services import ai_service
    msg = ai_service.chat_completion(messages, block=True)
    return (msg.get("content") or ""), (msg.get("usage") or {})


def start_workflow_run(workflow, user, params, trigger_file_id=None):
    """Launch an automation run for a workflow card; returns the run row."""
    runtime = _automation_runtime()
    return runtime.start_run(workflow, user, trigger="dashboard",
                             trigger_file_id=trigger_file_id,
                             params=params or None)


def iter_workflow_events(stored, card, user):
    """Run a workflow card, yielding per-node progress frames then done.

    Frames mirror ``iter_run_events`` (``{"t": "step", index, label,
    detail, card_id}`` per fresh automation log entry, then ``{"t":
    "done", "result", "trace", "artifact_ids", "card_id"}``). A missing
    workflow or bad params raises MissingInputError; a run that vanishes
    mid-wait (deleting a workflow cascades its runs) raises ValueError —
    the stream route settles both onto the card as error and closes.
    """
    doc = doc_service.load(stored)
    files = resolve_inputs(stored, doc, card)
    workflow_ref = (card.get("inputs") or {}).get("workflow") or {}
    try:
        workflow_id = int(workflow_ref.get("workflow_id"))
    except (TypeError, ValueError):
        raise MissingInputError(
            "This card has no valid workflow reference — "
            "re-open the Builder and pick a workflow.")
    models = _automation_models()
    workflow = (models.AutomationWorkflow.query
                .filter_by(id=workflow_id, user_id=user.id).first())
    if workflow is None:
        raise MissingInputError(
            f"Missing input: workflow #{workflow_id} was deleted or is not "
            f"yours — pick another workflow in the Builder.")
    params = workflow_ref.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("Card workflow params must be an object.")
    workflow_name = workflow.name
    run = start_workflow_run(workflow, user, params,
                             trigger_file_id=workflow_ref.get(
                                 "trigger_file_id"))
    seen = 0
    deadline = time.monotonic() + RUN_POLL_TIMEOUT_S
    while True:
        # populate_existing: the worker commits on its own session, so the
        # identity-mapped row would read stale without a forced re-fetch; a
        # deleted workflow cascades its runs, which then reads as None.
        live = (db.session.query(models.AutomationRun)
                .populate_existing()
                .filter_by(id=run.id, user_id=user.id).first())
        if live is None:
            raise ValueError(f"Workflow run #{run.id} disappeared.")
        try:
            entries = json.loads(live.log or "[]")
        except ValueError:
            entries = []
        if not isinstance(entries, list):
            entries = []
        for pos in range(seen, len(entries)):
            entry = entries[pos] if isinstance(entries[pos], dict) else {}
            yield {"t": "step", "index": pos,
                   "label": entry.get("label") or entry.get("node") or "node",
                   "detail": entry.get("summary") or entry.get("status") or "",
                   "card_id": card.get("id")}
        seen = len(entries)
        if live.status not in ("queued", "running"):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Workflow run #{run.id} did not finish in time.")
        time.sleep(RUN_POLL_INTERVAL_S)
    result, artifact_ids = _collect_workflow_result(
        live, workflow_name, card, user)
    yield {"t": "done", "card_id": card.get("id"), "result": result,
           "trace": [], "artifact_ids": artifact_ids}


def _collect_workflow_result(live, workflow_name, card, user):
    """Turn a finished automation run into a card result + artifacts.

    Raises ValueError when the run did not finish cleanly (the caller
    surfaces it on the card); returns (result_dict, [artifact_file_ids]).
    """
    if live.status != "done":
        detail = (live.error or f"the run {live.status}").strip()
        raise ValueError(
            f"Workflow '{workflow_name}' {live.status}: {detail} "
            f"(hint: re-open the Builder if the workflow changed).")
    try:
        output = json.loads(live.output or "{}")
    except ValueError:
        output = {}
    artifact_ids = []
    for ref in output.get("files") or []:
        fid = ref.get("file_id") if isinstance(ref, dict) else None
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        target = db.session.get(StoredFile, fid)
        if (target is not None and target.user_id == user.id
                and target.deleted_at is None and fid not in artifact_ids):
            artifact_ids.append(fid)
    prompt = (card.get("prompt") or "").strip()
    text = None
    if prompt:
        summary = json.dumps(
            output.get("results", []))[:8000]
        text, usage = complete_once(
            user, [{"role": "user",
                    "content": f"{prompt}\n\n--- Workflow output ---\n"
                               f"{summary}"}])
        _record_usage(user.id, [usage])
    return {"workflow": {"run_id": live.id,
                         "results": output.get("results", []),
                         "files": output.get("files") or []},
            "text": text}, artifact_ids


def _pyrun_evaluate(code, files):
    """Evaluate a script in the pyrun sandbox (stubbed in tests)."""
    from docindex_module_pyrun.evaluate import evaluate
    return evaluate(code, files=files)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def resolve_inputs(stored, doc, card):
    """Map a card's attachment refs to StoredFiles.

    Raises MissingInputError naming the input when the attachment entry is
    unknown or its file was trashed, moved or deleted — never silent.
    """
    refs = (card.get("inputs") or {}).get("attachments") or []
    entries = {a.get("id"): a for a in doc.get("attachments", [])}
    files = {}
    for ref in refs:
        entry = entries.get(ref)
        label = (entry or {}).get("name") or ref
        if entry is None:
            raise MissingInputError(
                f"Attachment '{label}' is no longer on this dashboard — "
                f"re-attach the file or remove it from the card.")
        target = db.session.get(StoredFile, entry.get("fileId"))
        if (target is None or target.user_id != stored.user_id
                or target.deleted_at is not None):
            raise MissingInputError(
                f"Missing input: '{label}' was trashed, moved or deleted — "
                f"restore the file or detach it from the dashboard.")
        files[ref] = target
    return files


def build_context(stored, doc, card, files):
    """Inline attached file text up to TOOLS_OFF_MAX_CHARS.

    Returns (context_text, truncated). Truncation is flagged on the card so
    tools-off runs stay honest about what the model actually saw. Spreadsheet
    attachments are binary workbooks — a raw text read is zip garbage, so
    they (and any attachment that reads back blank) fall back to the indexed
    extracted text instead of inlining garbage.
    """
    refs = (card.get("inputs") or {}).get("attachments") or []
    chunks, used, truncated = [], 0, False
    for ref in refs:
        stored_file = files[ref]
        try:
            text = file_service.read_text_content(
                stored_file, max_chars=TOOLS_OFF_MAX_CHARS + 1)
        except OSError as exc:
            raise MissingInputError(
                f"Attachment '{stored_file.name}' cannot be read ({exc}).")
        if ((stored_file.extension or "").lower() in _SPREADSHEET_EXTS
                or not text.strip()):
            try:
                extracted = indexing_service.extract_text(stored_file)
            except Exception:  # noqa: BLE001 - fall back to the raw read
                extracted = ""
            if extracted and extracted.strip():
                text = extracted
        room = TOOLS_OFF_MAX_CHARS - used
        if room <= 0:
            truncated = True
            continue
        if len(text) > room:
            text, truncated = text[:room], True
        chunks.append(f"--- File: {stored_file.name} "
                      f"(id {stored_file.id}) ---\n{text}")
        used += len(text)
    return "\n\n".join(chunks), truncated


def _record_usage(user_id, usages):
    for usage in usages:
        if isinstance(usage, dict):
            ai_usage_service.record_usage(
                user_id, usage, model=usage.get("model"),
                kind="dashboard")


# ---------------------------------------------------------------------------
# Card execution — returns (result_dict, [new_artifact_file_ids])
# ---------------------------------------------------------------------------

def run_card(stored, card, user_id):
    """Execute one card synchronously.

    Returns (result_dict, [new_artifact_file_ids], trace). Raises
    MissingInputError for gone inputs; other failures propagate and
    the caller turns them into the card's visible error state.
    """
    from app.models import User
    user = db.session.get(User, user_id)
    if user is None:
        raise ValueError("Dashboard owner not found.")
    doc = doc_service.load(stored)
    files = resolve_inputs(stored, doc, card)
    workflow_ref = (card.get("inputs") or {}).get("workflow") or {}
    if card.get("type") == "workflow" or workflow_ref.get("workflow_id"):
        result, artifact_ids = _run_workflow_card(stored, doc, card, user,
                                                 workflow_ref, files)
        return result, artifact_ids, []
    if card.get("script_enabled") and card.get("type") != "workflow":
        from app.services import module_service
        if not module_service.is_enabled("pyrun"):
            raise ValueError("The pyrun module is disabled — enable pyrun "
                             "to run script cards.")
        result, trace = _run_script_card(stored, doc, card, user, files)
        return result, [], trace
    return _run_prompt_card(stored, doc, card, user, files)


def _run_prompt_card(stored, doc, card, user, files):
    prompt = (card.get("prompt") or "").strip()
    if not prompt and not files:
        raise ValueError("Card has no prompt and no inputs — nothing to run.")
    refs = (card.get("inputs") or {}).get("attachments") or []
    contract = _output_contract(card.get("type"))
    if card.get("tools", True):
        lines = [prompt] if prompt else ["Summarize the attached files."]
        if refs:
            lines.append("")
            lines.append("Attached files (read them with the file tools):")
            for ref in refs:
                target = files[ref]
                lines.append(f"- {target.name} (file_id {target.id})")
        if contract:
            lines += ["", contract]
        drive = stored.drive
        answer, usages, trace = run_agent_collected(
            user, "\n".join(lines),
            drive=[drive] if drive is not None else None,
            max_steps=card.get("maxSteps"))
        _record_usage(user.id, usages)
        return _parse_prompt_result(card.get("type"), answer), [], trace
    context, truncated = build_context(stored, doc, card, files)
    body = prompt or "Summarize the attached files."
    if context:
        body += "\n\n--- Attached context ---\n" + context
    if contract:
        body += "\n\n" + contract
    text, usage = complete_once(user, [{"role": "user", "content": body}])
    _record_usage(user.id, [usage])
    result = _parse_prompt_result(card.get("type"), text)
    if truncated:
        result = dict(result, truncated=True)
    # Single LLM call: no tool steps, so the trace stays empty.
    return result, [], []


def _run_script_card(stored, doc, card, user, files):
    """Execute a script card in the pyrun sandbox, no prompt, no AI steps.

    Attachments map to pyrun files positionally in card attachment order:
    attachment i is addressable as f"f{i}" (f0, f1, ...) — each a
    [read_text_content] chunk list, so CSV/XLSX attachments seed scratch
    tables t0..tN in the same order. There is deliberately no "f" alias:
    pyrun seeds tables in files-dict order, so an extra alias key would
    duplicate the first file at t1 and shift every later table. The value
    coerces into the card type's snapshot contract; script errors raise
    ValueError for the card's visible error state.
    """
    del stored, doc, user  # ownership/files resolved by the caller
    script = card.get("script") or ""
    if not script.strip():
        raise ValueError("Script card has no script — add one or turn "
                         "the toggle off.")
    refs = (card.get("inputs") or {}).get("attachments") or []
    files_map = {}
    for pos, ref in enumerate(refs):
        target = files[ref]
        try:
            chunks = [file_service.read_text_content(target)]
        except OSError as exc:
            raise MissingInputError(
                f"Attachment '{target.name}' cannot be read ({exc}).")
        files_map[f"f{pos}"] = chunks
    started = time.perf_counter()
    outcome = _pyrun_evaluate(script, files_map)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if not isinstance(outcome, dict) or not outcome.get("ok"):
        error = outcome.get("error") if isinstance(outcome, dict) else None
        raise ValueError(error or "Script run failed.")
    result = _parse_script_result(card.get("type"), outcome.get("value"))
    inputs = ", ".join(f"{files[ref].name}→t{pos}"
                       for pos, ref in enumerate(refs)) or "none"
    trace = [{"index": None, "label": "script",
              "detail": "inputs: " + inputs},
             {"index": None, "label": "timing",
              "detail": f"finished in {elapsed_ms} ms"}]
    return result, trace


def _run_workflow_card(stored, doc, card, user, workflow_ref, files):
    # Single execution path with the streamed variant: consume the event
    # iterator and keep the final frame (progress frames feed the live
    # console only; the snapshot settles result + artifacts). Inputs are
    # re-resolved inside the iterator; the pre-resolved args stay in the
    # signature for the run_card call shape.
    del doc, workflow_ref, files
    result, artifact_ids = None, []
    for frame in iter_workflow_events(stored, card, user):
        if frame.get("t") == "done":
            result = frame.get("result")
            artifact_ids = frame.get("artifact_ids") or []
    return result, artifact_ids


def _wait_for_run(run_id, user_id):
    """Reload an automation run until it leaves queued/running."""
    models = _automation_models()
    deadline = time.monotonic() + RUN_POLL_TIMEOUT_S
    while True:
        run = (models.AutomationRun.query
               .filter_by(id=run_id, user_id=user_id).first())
        if run is None:
            raise ValueError(f"Workflow run #{run_id} disappeared.")
        db.session.refresh(run)
        if run.status not in ("queued", "running"):
            return run
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Workflow run #{run_id} did not finish in time.")
        time.sleep(RUN_POLL_INTERVAL_S)
