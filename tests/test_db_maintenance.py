"""Unknown migration revisions must not trigger schema or stamp repairs."""

import runpy
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from app.extensions import db, migrate
from app.services import db_maintenance


@pytest.mark.parametrize("revision", ["d7e9f3a52b01", "gitsync_0001"])
@pytest.mark.parametrize("operation", ["repair_unknown_revision", "auto_upgrade"])
def test_unknown_revision_is_preserved(app, monkeypatch, revision, operation):
    with app.app_context():
        db.session.execute(sa.text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        db.session.execute(sa.text(
            "INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision})
        db.session.commit()
        tables_before = set(sa.inspect(db.engine).get_table_names())

        def unexpected_create_all():
            pytest.fail("Unknown revisions must not create tables")

        monkeypatch.setattr(db, "create_all", unexpected_create_all)
        with pytest.raises(CommandError, match="[Bb]ack up"):
            getattr(db_maintenance, operation)(app)

        assert db.session.execute(sa.text(
            "SELECT version_num FROM alembic_version")).scalars().all() == [revision]
        assert set(sa.inspect(db.engine).get_table_names()) == tables_before


def test_auto_upgrade_uses_all_heads(app, monkeypatch):
    calls = []
    monkeypatch.setattr(db_maintenance, "_upgrade",
                        lambda **kwargs: calls.append(kwargs))
    db_maintenance.auto_upgrade(app)
    assert calls == [{"revision": "heads"}]


def test_auto_upgrade_propagates_other_errors(app, monkeypatch):
    def fail(**kwargs):
        raise CommandError("migration SQL failed")

    monkeypatch.setattr(db_maintenance, "_upgrade", fail)
    with pytest.raises(CommandError, match="migration SQL failed"):
        db_maintenance.auto_upgrade(app)


def test_gitsync_original_migration_upgrades_and_preserves_data(app, user, tmp_path, monkeypatch):
    """Exercise the original migration from commit 25ec285, not a no-op alias."""
    versions = tmp_path / "versions"
    versions.mkdir()
    versions.joinpath("gitsync_0001_repositories.py").write_text('''
import sqlalchemy as sa
from alembic import op

revision = "gitsync_0001"
down_revision = None
branch_labels = ("gitsync",)
depends_on = None

def upgrade():
    op.create_table(
        "mod_gitsync_repository",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("drive_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("account", sa.String(120), nullable=False),
        sa.Column("baseline", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["drive_id"], ["drives.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "path"),
    )

def downgrade():
    op.drop_table("mod_gitsync_repository")
''', encoding="utf-8")
    with app.app_context():
        config = migrate.get_config()
        current_heads = set(ScriptDirectory.from_config(config).get_heads())
        db.session.execute(sa.text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        for revision in current_heads:
            db.session.execute(sa.text(
                "INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": revision})
        db.session.commit()
        config.set_main_option("version_locations",
                               config.get_main_option("version_locations") + " " + str(versions))
        monkeypatch.setattr(migrate, "get_config", lambda: config)

        db_maintenance.auto_upgrade(app)
        assert "mod_gitsync_repository" in sa.inspect(db.engine).get_table_names()
        db.session.execute(sa.text(
            "INSERT INTO mod_gitsync_repository (id, user_id, name, path, account) "
            "VALUES (1, :user_id, 'test', '/test', 'local')"), {"user_id": user})
        db.session.commit()

        db_maintenance.auto_upgrade(app)
        assert set(db.session.execute(sa.text(
            "SELECT version_num FROM alembic_version")).scalars()) == current_heads | {"gitsync_0001"}
        assert db.session.execute(sa.text(
            "SELECT name FROM mod_gitsync_repository WHERE id = 1")).scalar() == "test"


@pytest.mark.parametrize("script", ["update.bat", "update.sh"])
def test_update_scripts_do_not_attempt_repair(script):
    source = (Path(__file__).resolve().parents[1] / script).read_text(encoding="utf-8")
    failure_branch = source.split("db upgrade heads", 1)[1]
    assert "db-repair" not in source
    assert "stamp heads" not in source
    assert "No automatic re-stamping was attempted" in failure_branch
    exit_command = "exit /b 1" if script.endswith(".bat") else "exit 1"
    assert exit_command in failure_branch


def test_legacy_repair_cli_refuses_unverified_repair(app, monkeypatch):
    import app as app_package

    monkeypatch.setattr(app_package, "create_app", lambda: app)
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "run.py"))
    result = app.test_cli_runner().invoke(args=["db-repair"])
    assert result.exit_code == 1
    assert "Automatic migration re-stamping is disabled" in result.output
    assert "Database repaired" not in result.output
