"""Git Sync module."""


def register(ctx):
    from . import models, routes  # noqa: F401

    ctx.blueprint = routes.bp
    return ctx.blueprint