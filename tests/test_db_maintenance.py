"""Recovery for databases stamped with a migration revision that no longer
exists (first public release, whose migration ids were later regenerated).
"""

import sqlalchemy as sa
from alembic.script import ScriptDirectory

from app.extensions import db, migrate
from app.services import db_maintenance


def _current_heads():
    cfg = migrate.get_config()
    return set(ScriptDirectory.from_config(cfg).get_heads())


def test_repair_unknown_revision_stamps_head(app):
    with app.app_context():
        # Simulate a first-release database: a version stamp alembic cannot
        # resolve against the current migration files.
        db.session.execute(sa.text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        db.session.execute(sa.text(
            "INSERT INTO alembic_version (version_num) VALUES ('d7e9f3a52b01')"))
        db.session.commit()

        db_maintenance.repair_unknown_revision(app)

        stamps = {r[0] for r in db.session.execute(
            sa.text("SELECT version_num FROM alembic_version")).all()}
        assert stamps == _current_heads()


def test_auto_upgrade_passes_unknown_stamp_through_repair(app, monkeypatch):
    calls = {"repair": 0}
    monkeypatch.setattr(db_maintenance, "repair_unknown_revision",
                        lambda a: calls.__setitem__("repair", calls["repair"] + 1))
    # The in-memory test DB has no alembic_version table at all, so upgrade
    # raises "Can't locate revision"-free errors normally — force the exact
    # failure mode instead.
    from alembic.util.exc import CommandError

    def boom(*a, **k):
        raise CommandError("Can't locate revision identified by 'd7e9f3a52b01'")

    monkeypatch.setattr(db_maintenance, "_upgrade", boom)
    db_maintenance.auto_upgrade(app)
    assert calls["repair"] == 1
