"""Drive trash: Drive.deleted_at + User.trash_auto_purge.

Revision ID: c3d4e5f6a7b8
Revises: 9a1b2c3d4e55
Create Date: 2026-10-20 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
down_revision = '9a1b2c3d4e55'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("drives") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_drives_deleted_at", ["deleted_at"])
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("trash_auto_purge", sa.Boolean(),
                                  nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("trash_auto_purge")
    with op.batch_alter_table("drives") as batch:
        batch.drop_index("ix_drives_deleted_at")
        batch.drop_column("deleted_at")
