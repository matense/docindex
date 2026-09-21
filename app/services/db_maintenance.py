"""Database schema maintenance: startup auto-upgrade with self-healing.

Databases created by the very first public release carry alembic_version
stamps that point at migration revisions which no longer exist (the
migration history was regenerated once before the project went public).
Alembic then fails hard with "Can't locate revision identified by ...".

Since the stamp no longer tells us which schema the database actually has,
the recovery is best-effort and safe for existing data:
  1. db.create_all()  — creates any missing tables, a no-op for existing ones
  2. stamp heads      — re-anchor the version table at the current head
Column-level drift on existing tables cannot be repaired this way; a warning
is logged so the user knows to report it if the app misbehaves.
"""

from alembic.util.exc import CommandError
from flask_migrate import stamp as _stamp
from flask_migrate import upgrade as _upgrade

from ..extensions import db


def auto_upgrade(app):
    """Bring the DB to the migration head; self-heal unknown stamps.

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
            app.logger.warning(
                "Database is stamped with a migration revision that no "
                "longer exists — re-anchoring to the current head. (%s)", exc)
            repair_unknown_revision(app)


def repair_unknown_revision(app):
    """Recovery for 'Can't locate revision': create any missing tables and
    stamp the current head. db.create_all() never touches existing tables,
    so existing data is preserved."""
    db.create_all()
    # purge=True drops the stale version rows first — otherwise alembic tries
    # to resolve the unknown stamp while computing the delta and fails again.
    _stamp(revision="heads", purge=True)
    app.logger.warning(
        "Database re-stamped at the current migration head. If the app "
        "misbehaves (missing columns), back up instance/ and report the "
        "issue.")
