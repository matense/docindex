"""module_states table

Admin-managed enable/disable state for extension modules. Modules absent
from this table are disabled by default.

Revision ID: b2e4f6a81c53
Revises: a9d3e7b15c02
Create Date: 2026-09-13 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2e4f6a81c53'
down_revision = 'a9d3e7b15c02'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "module_states",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("enabled_by", sa.Integer(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["enabled_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade():
    op.drop_table("module_states")
