"""Node-type registry for the Automation module.

A node type couples a spec (palette label/icon, ports, config fields — the
editor renders forms straight from it) with an executor callable. Other
modules can add their own node types via ``register_node_type(...,
module="<name>")``; node types of disabled modules are hidden from the
palette and fail closed at execution time, mirroring the agent tool
registry.
"""

_NODE_TYPES = {}

# Palette section order. Categories not listed here sort after these,
# keeping their registration order (the sort below is stable).
CATEGORY_ORDER = ["Input", "AI", "Transform", "Data", "Logic", "Output"]


def register_node_type(type_name, spec, executor, module="automation"):
    """Register a node type.

    ``spec``: {label, icon, category, inputs: [{name, kind}], outputs:
    [{name, kind}], fields: [{name, label, kind, default, help, options?}]}.
    ``executor``: fn(ctx, config, inputs) -> payload | {port: payload|None}.
      - ctx: {"user", "log", "app"} (log(node_free_text) appends a note to
        the node's run-log entry).
      - config: the node's configured field values.
      - inputs: {port_name: payload} — only ports that received data.
      - Return a single payload (single-output nodes) or a dict mapping
        output ports to payloads; None marks the port as not taken
        (logic.condition's false branch).
    """
    if type_name in _NODE_TYPES:
        raise ValueError(f"Duplicate automation node type: {type_name}")
    _NODE_TYPES[type_name] = {
        "spec": spec, "executor": executor, "module": module,
    }


def _module_active(module):
    if module == "automation":
        # The module's own types: reachability is already gated by the
        # per-request guard and the scheduler's is_enabled check.
        return True
    from app.services import module_service
    return module_service.is_enabled(module)


def active_node_types():
    """Node types usable right now (their owning module is enabled)."""
    return {t: e for t, e in _NODE_TYPES.items() if _module_active(e["module"])}


def palette():
    """Specs of active node types, grouped for the editor palette."""
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    specs = [{"type": t, **e["spec"]} for t, e in active_node_types().items()]
    specs.sort(key=lambda s: order.get(s.get("category"), len(order)))
    return specs
