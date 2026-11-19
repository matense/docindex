"""Whiteboard organize plans: WhiteboardOrganizePlan rows.

Revision ID: f4a5b6c7d8e9
Revises: e8f9a0b1c2d3
Create Date: 2026-11-19 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f4a5b6c7d8e9'
down_revision = 'e8f9a0b1c2d3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "whiteboard_organize_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False, index=True),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("reverted", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table("whiteboard_organize_plans")
