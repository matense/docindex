"""Notebooks module — Jupyter-style cell documents stored as .pdocnb files."""

DEFAULT_DRIVE_NAME = "My Notebooks"


def register(ctx):
    from . import doc, routes, tools  # noqa: F401 - imports do the wiring
    from app.services import agent_service, indexing_service, module_service

    indexing_service.register_extractor(doc.EXTENSION, doc.extract)
    module_service.register_file_viewer(
        doc.EXTENSION,
        lambda stored: f"/m/notebooks/{stored.id}",
        module="notebooks")
    # AI-hidden notebooks are invisible to every core AI tool.
    agent_service.register_file_guard(doc.ai_file_guard, module="notebooks")
    ctx.blueprint = routes.bp
    return ctx.blueprint


def on_enable(user):
    """First-enable hook: give the user a dedicated notebooks drive."""
    from app.extensions import db
    from app.models import Drive

    if not Drive.query.filter_by(
            user_id=user.id, name=DEFAULT_DRIVE_NAME).first():
        db.session.add(Drive(name=DEFAULT_DRIVE_NAME, user_id=user.id,
                             description="Your notebooks"))
        db.session.commit()
