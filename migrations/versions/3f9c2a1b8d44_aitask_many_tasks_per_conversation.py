"""ai_tasks: many tasks per conversation + prompt

Background mode now continues the same conversation instead of starting a
new one, so one conversation can own several sequential tasks. Drops the
UNIQUE constraint on ai_tasks.conversation_id (kept as a plain index) and
stores the exact launching question in ai_tasks.prompt (title is truncated).

SQLite cannot drop a UNIQUE constraint in place, so the table is rebuilt
(copy, drop, rename).

Revision ID: 3f9c2a1b8d44
Revises: c1a2b3d4e5f6
Create Date: 2026-10-18 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3f9c2a1b8d44'
down_revision = 'c1a2b3d4e5f6'
branch_labels = None
depends_on = None

_COLS = ("id, user_id, conversation_id, title, prompt, status, error, "
         "notified, context, created_at, started_at, finished_at")
_COLS_OLD = ("id, user_id, conversation_id, title, NULL, status, error, "
             "notified, context, created_at, started_at, finished_at")


def _create(name, unique_conv):
    op.create_table(
        name,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"),
                  nullable=False, index=True),
        sa.Column("conversation_id", sa.Integer(),
                  sa.ForeignKey("chat_conversations.id"), nullable=False,
                  unique=unique_conv, index=True),
        sa.Column("title", sa.String(255), nullable=False,
                  server_default="Background task"),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="queued", index=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )


def upgrade():
    _create("ai_tasks_new", unique_conv=False)
    op.execute(f"INSERT INTO ai_tasks_new ({_COLS}) SELECT {_COLS_OLD} "
               "FROM ai_tasks")
    op.drop_table("ai_tasks")
    op.rename_table("ai_tasks_new", "ai_tasks")


def downgrade():
    # Re-adds the UNIQUE constraint; fails if a conversation owns several
    # tasks — delete the extras first.
    _create("ai_tasks_new", unique_conv=True)
    op.execute(f"INSERT INTO ai_tasks_new ({_COLS}) SELECT {_COLS} "
               "FROM ai_tasks")
    op.drop_table("ai_tasks")
    op.rename_table("ai_tasks_new", "ai_tasks")
