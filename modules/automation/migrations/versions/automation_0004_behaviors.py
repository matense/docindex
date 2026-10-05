"""automation module: AI behaviors library

Reusable system prompts the user manages on the Behaviors page and picks
in the AI nodes' behavior field; the AI chat can also create them via the
automation.save_behavior tool.

Revision ID: automation_0004
Revises: automation_0003
Create Date: 2026-10-04 10:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'automation_0004'
down_revision = 'automation_0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mod_automation_behavior",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mod_automation_behavior_user_id",
                    "mod_automation_behavior", ["user_id"])


def downgrade():
    op.drop_index("ix_mod_automation_behavior_user_id",
                  table_name="mod_automation_behavior")
    op.drop_table("mod_automation_behavior")
