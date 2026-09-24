"""AITask.context

Background tasks remember the context they were launched with (drives,
attached files, open file, notebook cell) as a JSON snapshot.

Revision ID: f1c6a9e25d83
Revises: e9b5d2a47c31
Create Date: 2026-09-21 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1c6a9e25d83'
down_revision = 'e9b5d2a47c31'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_tasks") as batch:
        batch.add_column(sa.Column("context", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("ai_tasks") as batch:
        batch.drop_column("context")
