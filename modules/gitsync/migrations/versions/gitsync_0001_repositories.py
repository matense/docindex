"""Git Sync local repository connections."""

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