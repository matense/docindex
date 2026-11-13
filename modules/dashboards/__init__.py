"""Dashboards module — prompt-driven card pages over drive files."""

DEFAULT_DRIVE_NAME = "My Dashboards"


def register(ctx):
    from app.services import indexing_service, module_service

    from . import doc, routes, tools  # noqa: F401 - imports do the wiring

    indexing_service.register_extractor(doc.EXTENSION, doc.extract)
    module_service.register_file_viewer(
        doc.EXTENSION,
        lambda stored: f"/m/dashboards/{stored.id}",
        module="dashboards")
    ctx.blueprint = routes.bp
    return ctx.blueprint
