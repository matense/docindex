"""ai_tasks table

Long-horizon background AI tasks: agent runs decoupled from the HTTP
request, linked one-to-one to a chat conversation that holds the
persisted transcript.

Revision ID: e9b5d2a47c31
Revises: b2e4f6a81c53
Create Date: 2026-09-17 23:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e9b5d2a47c31'
down_revision = 'b2e4f6a81c53'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"),
                  nullable=False, index=True),
        sa.Column("conversation_id", sa.Integer(),
                  sa.ForeignKey("chat_conversations.id"), nullable=False,
                  unique=True),
        sa.Column("title", sa.String(255), nullable=False,
                  server_default="Background task"),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="queued", index=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table("ai_tasks")
