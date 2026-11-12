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
``complete_once``, ``start_workflow_run``) so tests can stub them; usage is
recorded through the existing AI usage events for every model call.
"""

import importlib
import json
import re
import threading
import time

from app.extensions import db
from app.models import StoredFile
from app.services import ai_usage_service, file_service

from . import doc as doc_service

# Tools-off context budget: attached files are inlined up to this many
# characters total; anything beyond is cut and flagged on the card.
TOOLS_OFF_MAX_CHARS = 20_000

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
            + _NO_FILE_REFS),
    "chart": ("--- Output contract (chart card) ---\n"
              "Reply with ONLY a JSON object, no prose. Shape: "
              '{"chart": {"kind": "bar|line|pie", "labels": [...up to 50...], '
              '"datasets": [{"label": "...", "data": [numbers...]}]}}. '
              + _NO_FILE_REFS),
    "list": ("--- Output contract (list card) ---\n"
             "Reply with one item per line, no numbering or bullets. "
             + _NO_FILE_REFS),
    "table": ("--- Output contract (table card) ---\n"
              "Reply as plain rows, one per line, columns separated by "
              "' | ', first line the header. " + _NO_FILE_REFS),
}

# Notebook chart-cell caps, mirrored for dashboard chart snapshots.
_CHART_MAX_LABELS = 50
_CHART_MAX_DATASETS = 500


def _output_contract(card_type):
    """The prompt suffix for a card type (empty for markdown/workflow)."""
    return _OUTPUT_CONTRACTS.get(card_type or "", "")


def _extract_json_block(answer):
    """First JSON object in an answer: fenced block, else whole text."""
    if not isinstance(answer, str):
        return None
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.S)
    candidate = match.group(1) if match else answer.strip()
    try:
        data = json.loads(candidate)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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
    except (TypeError, ValueError):
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
    kind = kind if kind in ("bar", "line", "pie") else "bar"
    labels = [str(label) for label in (spec.get("labels") or [])]
    labels = labels[:_CHART_MAX_LABELS]
    raw_datasets = spec.get("datasets") or []
    if not labels or not raw_datasets:
        return {"text": answer}
    datasets = []
    for entry in raw_datasets[:_CHART_MAX_DATASETS]:
        if not isinstance(entry, dict):
            continue
        values = [_coerce_number(v) for v in (entry.get("data") or [])]
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


def _parse_prompt_result(card_type, answer):
    """Structured snapshot for kpi/chart answers; {"text"} otherwise."""
    if card_type == "kpi":
        return _parse_kpi_result(answer)
    if card_type == "chart":
        return _parse_chart_result(answer)
    return {"text": answer}


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

def run_agent_answer(user, prompt, drive=None):
    """Full agent run with the tool loop; returns (answer_text, [usages])."""
    from app.services import agent_service
    answer, usages = "", []
    events = agent_service.run_agent_events(
        user, [{"role": "user", "content": prompt}], drive=drive)
    for kind, payload in events:
        if kind == "answer":
            answer = payload if isinstance(payload, str) else str(payload or "")
        elif kind == "usage" and isinstance(payload, dict):
            usages.append(payload)
    return answer, usages


def complete_once(user, messages):
    """Single LLM call; returns (text, usage dict)."""
    from app.services import ai_service
    msg = ai_service.chat_completion(messages, block=True)
    return (msg.get("content") or ""), (msg.get("usage") or {})


def start_workflow_run(workflow, user, params):
    """Launch an automation run for a workflow card; returns the run row."""
    runtime = _automation_runtime()
    return runtime.start_run(workflow, user, trigger="dashboard",
                             params=params or None)


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
    tools-off runs stay honest about what the model actually saw.
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

    Raises MissingInputError for gone inputs; other failures propagate and
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
        return _run_workflow_card(stored, doc, card, user, workflow_ref,
                                  files)
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
        answer, usages = run_agent_answer(
            user, "\n".join(lines),
            drive=[drive] if drive is not None else None)
        _record_usage(user.id, usages)
        return _parse_prompt_result(card.get("type"), answer), []
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
    return result, []


def _run_workflow_card(stored, doc, card, user, workflow_ref, files):
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
    run = start_workflow_run(workflow, user, params)
    run = _wait_for_run(run.id, user.id)
    if run.status != "done":
        detail = (run.error or f"the run {run.status}").strip()
        raise ValueError(
            f"Workflow '{workflow.name}' {run.status}: {detail} "
            f"(hint: re-open the Builder if the workflow changed).")
    try:
        output = json.loads(run.output or "{}")
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
    return {"workflow": {"run_id": run.id,
                         "results": output.get("results", []),
                         "files": output.get("files", [])},
            "text": text}, artifact_ids


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
