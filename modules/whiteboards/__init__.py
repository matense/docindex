"""Whiteboards module — Excalidraw scenes stored as .wboard files."""

DEFAULT_DRIVE_NAME = "My Whiteboards"


def register(ctx):
    from app.services import agent_service, indexing_service, module_service

    from . import board, routes, tools  # noqa: F401 - imports do the wiring

    indexing_service.register_extractor(board.EXTENSION, board.extract)
    module_service.register_file_viewer(
        board.EXTENSION,
        lambda stored: f"/m/whiteboards/{stored.id}",
        module="whiteboards")
    # AI-hidden boards are invisible to every core AI tool.
    agent_service.register_file_guard(board.ai_file_guard,
                                      module="whiteboards")
    ctx.blueprint = routes.bp
    return ctx.blueprint


def on_enable(user):
    """First-enable hook: give the user a dedicated whiteboards drive."""
    from app.extensions import db
    from app.models import Drive

    if not Drive.query.filter_by(
            user_id=user.id, name=DEFAULT_DRIVE_NAME).first():
        db.session.add(Drive(name=DEFAULT_DRIVE_NAME, user_id=user.id,
                             description="Your whiteboards"))
        db.session.commit()
