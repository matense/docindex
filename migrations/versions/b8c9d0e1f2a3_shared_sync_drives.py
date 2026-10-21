"""Writable shared sync drives: Drive.sync_mode + sync_ignored.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-10-21 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("drives") as batch:
        batch.add_column(sa.Column("sync_mode", sa.String(20), nullable=False,
                                   server_default="mirror"))
        batch.add_column(sa.Column("sync_ignored", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("drives") as batch:
        batch.drop_column("sync_ignored")
        batch.drop_column("sync_mode")
