"""User.shift_enter_to_send preference (Shift+Enter sends, Enter newline).

Revision ID: c7d8e9f0a1b2
Revises: f2a3b4c5d6e7
Create Date: 2026-10-26 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7d8e9f0a1b2'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("shift_enter_to_send", sa.Boolean(),
                                   nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("shift_enter_to_send")
