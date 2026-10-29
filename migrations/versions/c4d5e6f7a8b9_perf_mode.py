"""User.perf_mode preference (performance mode: lite visuals, calm timers).

Revision ID: c4d5e6f7a8b9
Revises: e2f3a4b5c6d7
Create Date: 2026-10-29 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("perf_mode", sa.Boolean(),
                                   nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("perf_mode")
