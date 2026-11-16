"""Dashboards module — prompt-driven card pages over drive files."""

DEFAULT_DRIVE_NAME = "My Dashboards"


def register(ctx):
    from app.services import agent_service, indexing_service, module_service

    from . import doc, routes, tools  # noqa: F401 - imports do the wiring

    indexing_service.register_extractor(doc.EXTENSION, doc.extract)
    module_service.register_file_viewer(
        doc.EXTENSION,
        lambda stored: f"/m/dashboards/{stored.id}",
        module="dashboards")
    # AI-hidden dashboards are invisible to every core AI tool.
    agent_service.register_file_guard(doc.ai_file_guard,
                                      module="dashboards")
    ctx.blueprint = routes.bp
    return ctx.blueprint


def on_enable(user):
    """First-enable hook: give the user a dedicated dashboards drive."""
    from app.extensions import db
    from app.models import Drive

    if not Drive.query.filter_by(
            user_id=user.id, name=DEFAULT_DRIVE_NAME).first():
        db.session.add(Drive(name=DEFAULT_DRIVE_NAME, user_id=user.id,
                             description="Your dashboards"))
        db.session.commit()
