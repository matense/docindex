"""ChatMessage.archived

Summarize-and-reset no longer deletes the old history: messages are marked
archived=True — still visible in the UI (grayed out) but excluded from the
context sent to the model.

Revision ID: e7b1d4f32a59
Revises: d6a9c3e21f48
Create Date: 2026-09-07 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7b1d4f32a59'
down_revision = 'd6a9c3e21f48'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("chat_messages") as batch:
        batch.add_column(sa.Column("archived", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("chat_messages") as batch:
        batch.drop_column("archived")
