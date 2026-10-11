"""Automation module — visual workflow builder and execution engine.

Workflows are DAGs of typed nodes (inputs, transforms, AI prompts, logic,
outputs) edited on a drag & drop canvas and executed server-side: manually,
on a schedule, or from the AI chat. Runs are persistent rows with a
per-node log, cooperative stop and startup recovery — the same pattern as
the core long-horizon AI tasks.
"""


def register(ctx):
    from . import models, nodes, routes, tools  # noqa: F401 - imports wire it up

    nodes.register_defaults()
    tools.register_tools()
    from . import runtime, scheduler
    try:
        runtime.recover_interrupted(ctx.app)
    except Exception:  # noqa: BLE001 - tables may not exist yet (tests
        # build them with db.create_all AFTER app creation); the run page
        # still works and the next startup recovers.
        ctx.logger.info("Automation run recovery deferred (tables not ready).")
    scheduler.start(ctx.app)

    ctx.blueprint = routes.bp
    return ctx.blueprint
