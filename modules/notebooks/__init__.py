"""Notebooks module — Jupyter-style cell documents stored as .pdocnb files."""


def register(ctx):
    from . import doc, routes, tools  # noqa: F401 - imports do the wiring
    from app.services import indexing_service, module_service

    indexing_service.register_extractor(doc.EXTENSION, doc.extract)
    module_service.register_file_viewer(
        doc.EXTENSION,
        lambda stored: f"/m/notebooks/{stored.id}",
        module="notebooks")
    ctx.blueprint = routes.bp
    return ctx.blueprint
