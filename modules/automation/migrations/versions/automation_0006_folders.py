"""automation module: organizer folders for workflows

A simple user-owned tree (AutomationFolder with self-referencing parent)
plus AutomationWorkflow.folder_id (NULL = root). Lets users — and the AI —
group workflows instead of one flat list.

Revision ID: automation_0006
Revises: automation_0005
Create Date: 2026-10-17 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'automation_0006'
down_revision = 'automation_0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mod_automation_folder",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["mod_automation_folder.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mod_automation_folder_user_id",
                    "mod_automation_folder", ["user_id"])
    op.add_column("mod_automation_workflow",
                  sa.Column("folder_id", sa.Integer(), nullable=True))
    op.create_index("ix_mod_automation_workflow_folder_id",
                    "mod_automation_workflow", ["folder_id"])


def downgrade():
    op.drop_index("ix_mod_automation_workflow_folder_id",
                  table_name="mod_automation_workflow")
    op.drop_column("mod_automation_workflow", "folder_id")
    op.drop_index("ix_mod_automation_folder_user_id",
                  table_name="mod_automation_folder")
    op.drop_table("mod_automation_folder")
