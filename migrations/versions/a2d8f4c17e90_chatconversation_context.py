"""ChatConversation.context

Conversations remember the context of their latest message (scoped drives,
attached files, open file, notebook cell) as a JSON snapshot, so reopening
a past conversation restores the references the user had set up.

Revision ID: a2d8f4c17e90
Revises: f1c6a9e25d83
Create Date: 2026-09-22 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a2d8f4c17e90'
down_revision = 'f1c6a9e25d83'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("chat_conversations") as batch:
        batch.add_column(sa.Column("context", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("chat_conversations") as batch:
        batch.drop_column("context")
