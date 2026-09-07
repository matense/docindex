"""AIConnection.history_messages

Per-connection chat history size (user/assistant messages sent to the
model); NULL falls back to the global AI_HISTORY_MESSAGES (default 20).

Revision ID: d6a9c3e21f48
Revises: c5f8b2d14e77
Create Date: 2026-09-07 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd6a9c3e21f48'
down_revision = 'c5f8b2d14e77'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_connections") as batch:
        batch.add_column(sa.Column("history_messages", sa.Integer(),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table("ai_connections") as batch:
        batch.drop_column("history_messages")
