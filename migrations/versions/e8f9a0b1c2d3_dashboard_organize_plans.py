"""Dashboard organize plans: DashboardOrganizePlan rows.

Revision ID: e8f9a0b1c2d3
Revises: cf8c68bf5eef
Create Date: 2026-11-19 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e8f9a0b1c2d3'
down_revision = 'cf8c68bf5eef'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dashboard_organize_plans",
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
    op.drop_table("dashboard_organize_plans")
