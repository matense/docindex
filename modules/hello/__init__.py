"""Hello example module — registers a page, an AI tool and a model."""


def register(ctx):
    from . import models, routes, tools  # noqa: F401 - imports do the wiring

    ctx.blueprint = routes.bp
    return ctx.blueprint
