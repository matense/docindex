"""Git drives module — mirror a remote Git repo as a read-only drive."""

from . import routes, source  # noqa: F401 - imports do the wiring
from .source import GitDriveSource


def register(ctx):
    from app.services import drive_service

    drive_service.register_drive_source(GitDriveSource.kind,
                                        GitDriveSource())
    ctx.blueprint = routes.bp
    return ctx.blueprint
