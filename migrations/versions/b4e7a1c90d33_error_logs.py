"""error logs table

Central application log (error_logs): errors and warnings captured from
unhandled request exceptions, app.logger warnings (indexing, sync, OCR, ...)
and AI provider failures. Shown on the settings logs page.

Revision ID: b4e7a1c90d33
Revises: a3f5c8e91b02
Create Date: 2026-08-20 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4e7a1c90d33'
down_revision = 'a3f5c8e91b02'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "error_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("path", sa.String(length=255), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_error_logs_level", "error_logs", ["level"])
    op.create_index("ix_error_logs_source", "error_logs", ["source"])
    op.create_index("ix_error_logs_user_id", "error_logs", ["user_id"])
    op.create_index("ix_error_logs_created_at", "error_logs", ["created_at"])


def downgrade():
    op.drop_table("error_logs")
