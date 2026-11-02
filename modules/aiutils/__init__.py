"""AI Utils module — calculator, converters and text tools for the AI.

Registers namespaced tools (aiutils.*) plus a reference page. Pure
stdlib, no storage, no network.
"""


def register(ctx):
    from . import routes, tools  # noqa: F401 - imports do the wiring

    ctx.blueprint = routes.bp
    return ctx.blueprint
