"""AITask.max_steps: resolved per-run step budget snapshot.

Revision ID: d1e2f3a4b5c6
Revises: c7d8e9f0a1b2
Create Date: 2026-10-28 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = 'c7d8e9f0a1b2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_tasks") as batch:
        batch.add_column(sa.Column("max_steps", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("ai_tasks") as batch:
        batch.drop_column("max_steps")
