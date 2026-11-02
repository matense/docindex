"""Database upgrades without guessing the schema behind unknown revisions."""

from alembic import command
from alembic.util.exc import CommandError

from ..extensions import migrate


def _upgrade(revision):
    command.upgrade(migrate.get_config(), revision)


def auto_upgrade(app):
    """Bring the DB to the migration heads; reject unknown stamps.

    Alembic's upgrade is a no-op when the DB is already at head, so this
    runs on every startup: users who pull new code but forget
    `flask db upgrade` would otherwise get "no such column" 500s.
    """
    with app.app_context():
        try:
            _upgrade(revision="heads")
        except CommandError as exc:
            if "Can't locate revision" not in str(exc):
                raise
            raise CommandError(
                f"{exc}. Migration history is missing from this checkout. "
                "Restore the matching branch/module migration files before "
                "retrying. Do not stamp heads: schema compatibility has not "
                "been verified. Back up the database before recovery."
            ) from exc


def repair_unknown_revision(app):
    """Refuse unverified recovery, including calls from the legacy CLI."""
    raise CommandError(
        "Automatic migration re-stamping is disabled: an unknown revision "
        "may belong to another branch or a missing module. Back up the "
        "database and restore its matching migration history; schema "
        "compatibility must be verified before any manual recovery."
    )
