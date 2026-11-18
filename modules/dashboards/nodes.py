"""Automation node: dashboard.refresh (run dashboard cards from a workflow).

Registered from dashboards/__init__.py::register via
register_dashboard_nodes(). The node type carries module="dashboards", so the
palette hides it while the module is disabled; the executor additionally
gates on module_service.is_enabled("dashboards") and fails closed naming it
(defense in depth — the registry alone hides the type).
"""

import importlib
import time
import uuid

from app.extensions import db
from app.models import utcnow


def _auto_engine():
    """The automation engine (WorkflowError + payload helpers)."""
    for name in ("docindex_module_automation.engine",
                 "modules.automation.engine"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise RuntimeError("Automation module is not available.")


def _auto_registry():
    """The automation node-type registry."""
    for name in ("docindex_module_automation.registry",
                 "modules.automation.registry"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise RuntimeError("Automation module is not available.")


def _dash():
    """Dashboard doc + engine services (relative: no load-order hazard)."""
    from . import doc as doc_service
    from . import engine as engine_service
    return doc_service, engine_service


def _wanted_cards(config):
    """Target card ids: comma-separated text (or a list); empty = all."""
    raw = config.get("cards")
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw or "").split(",")
            if part.strip()]


def _settle(stored, doc_service, engine_service, target, status, result,
            trace, artifact_ids=(), ran_secs=None, run_id=None):
    """Persist one refreshed card (routes.run_cards settle shape)."""
    from app.models import StoredFile
    with engine_service.dashboard_lock(stored.id):
        fresh = doc_service.load(db.session.get(StoredFile, stored.id))
        card = next((c for c in fresh.get("cards", [])
                     if c.get("id") == target), None)
        if card is None:
            return
        if run_id is not None and card.get("runId") != run_id:
            # Superseded by a newer run (e.g. a UI run claimed the card
            # between our run_card and this settle): the newer writer owns
            # this card now — never clobber it.
            return
        card["status"] = status
        card["result"] = result
        card["trace"] = trace
        card["ranAt"] = utcnow().isoformat()
        if ran_secs is not None:
            card["ranSecs"] = ran_secs
        registry = fresh.setdefault("createdFileIds", [])
        for new_id in artifact_ids:
            if new_id not in registry:
                registry.append(new_id)
        doc_service.save_document(
            db.session.get(StoredFile, stored.id), fresh)


def _exec_dashboard_refresh(ctx, config, inputs):
    del inputs  # config-only output node: no inbound ports
    from app.services import module_service
    auto_engine = _auto_engine()
    WorkflowError = auto_engine.WorkflowError
    if not module_service.is_enabled("dashboards"):
        raise WorkflowError("The dashboards module is disabled — enable "
                            "dashboards to refresh dashboard cards.")
    doc_service, engine_service = _dash()
    raw_id = config.get("dashboard_id")
    try:
        dashboard_id = int(raw_id)
    except (TypeError, ValueError):
        raise WorkflowError(
            f"Dashboard #{raw_id} does not exist (or is in the trash)."
        ) from None
    stored = doc_service.get_dashboard(dashboard_id, ctx["user"].id)
    if stored is None:
        raise WorkflowError(
            f"Dashboard #{dashboard_id} does not exist (or is in the "
            "trash).")
    cards = doc_service.load(stored).get("cards", [])
    by_id = {c.get("id"): c for c in cards}
    wanted = _wanted_cards(config)
    targets = wanted or [c.get("id") for c in cards]
    targets = list(dict.fromkeys(targets))
    unknown = [t for t in targets if t not in by_id]
    if unknown:
        raise WorkflowError(f"Unknown card(s): {', '.join(unknown)}.")
    # Run-owned claim (routes.run_cards shape): mint one run id per target,
    # mark running + runId under the dashboard lock, then register the live
    # runs — the settle path below (and any concurrent UI run) settles by
    # run id, so a newer claim always wins over this older workflow write.
    run_ids = [f"{stored.id}:{uuid.uuid4().hex[:12]}" for _ in targets]
    with engine_service.dashboard_lock(stored.id):
        fresh_doc = doc_service.load(stored)
        live = {c.get("id") for c in fresh_doc.get("cards", [])}
        missing = [t for t in targets if t not in live]
        if missing:
            raise WorkflowError(
                f"Unknown card(s): {', '.join(missing)}.")
        for target, run_id in zip(targets, run_ids, strict=False):
            card = next(c for c in fresh_doc["cards"]
                        if c.get("id") == target)
            card["status"] = "running"
            card["runId"] = run_id
        doc_service.save_document(stored, fresh_doc)
        for run_id in run_ids:
            doc_service.run_started(stored.id, run_id)
    ran, failed = [], {}
    for target, run_id in zip(targets, run_ids, strict=False):
        card = next(c for c in doc_service.load(stored)["cards"]
                    if c.get("id") == target)
        started = time.monotonic()
        try:
            try:
                result, artifact_ids, trace = engine_service.run_card(
                    stored, card, ctx["user"].id)
            except Exception as exc:  # noqa: BLE001 - recorded, never raised
                db.session.rollback()
                error = (str(exc) or "The card run failed.")[:500]
                failed[target] = error
                _settle(stored, doc_service, engine_service, target, "error",
                        {"error": error}, [],
                        ran_secs=round(time.monotonic() - started, 1),
                        run_id=run_id)
                continue
            _settle(stored, doc_service, engine_service, target, "done",
                    result, trace, artifact_ids,
                    ran_secs=round(time.monotonic() - started, 1),
                    run_id=run_id)
            ctx["log"](f"refreshed card '{target}' "
                       f"in {time.monotonic() - started:.1f}s")
            ran.append(target)
        finally:
            doc_service.run_finished(stored.id, run_id)
    ctx["log"](f"{len(ran)} card(s) refreshed, {len(failed)} failed")
    return auto_engine.p_json({"ran": ran, "failed": failed})


def register_dashboard_nodes():
    """Register dashboard-owned automation node types (idempotent)."""
    registry = _auto_registry()
    if "dashboard.refresh" in registry._NODE_TYPES:
        return
    registry.register_node_type("dashboard.refresh", {
        "label": "Refresh dashboard", "icon": "fa-rotate",
        "category": "Output",
        "description": "Refreshes a dashboard (or specific cards) and "
                       "returns which cards ran — a failing card is "
                       "recorded, never aborts the workflow.",
        "example": "Refresh the KPI card of dashboard 12 after saving "
                   "the new sales table.",
        "inputs": [],
        "outputs": [{"name": "out", "kind": "json"}],
        "fields": [
            {"name": "dashboard_id", "label": "Dashboard", "kind": "text",
             "required": True, "default": "",
             "placeholder": "e.g. 12",
             "help": "The dashboard id (number). Only your own, "
                     "non-trashed dashboards can refresh."},
            {"name": "cards", "label": "Cards (optional)", "kind": "text",
             "default": "", "placeholder": "e.g. card-id-1, card-id-2",
             "advanced": True,
             "help": "Comma-separated card ids to refresh — empty runs "
                     "all cards."},
        ]}, _exec_dashboard_refresh, module="dashboards")
