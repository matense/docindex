"""AIConnection.long_horizon_steps: per-connection long-horizon budget.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-10-29 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_connections") as batch:
        batch.add_column(sa.Column("long_horizon_steps", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("ai_connections") as batch:
        batch.drop_column("long_horizon_steps")
