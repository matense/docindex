"""Table organize plans: TableOrganizePlan rows.

Revision ID: cf8c68bf5eef
Revises: 7d3f1a9b2c4e
Create Date: 2026-11-19 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'cf8c68bf5eef'
down_revision = '7d3f1a9b2c4e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "table_organize_plans",
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
    op.drop_table("table_organize_plans")
