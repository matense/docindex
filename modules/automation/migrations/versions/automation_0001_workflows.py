"""automation module: workflow and run tables

Independent alembic root for the automation module — module migrations do
not join the core chain so removing the module never breaks core upgrades.

Revision ID: automation_0001
Revises:
Create Date: 2026-09-28 22:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'automation_0001'
down_revision = None
branch_labels = ('automation',)
depends_on = None


def upgrade():
    op.create_table(
        "mod_automation_workflow",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("graph", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mod_automation_workflow_user_id",
                    "mod_automation_workflow", ["user_id"])
    op.create_table(
        "mod_automation_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("log", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workflow_id"], ["mod_automation_workflow.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mod_automation_run_workflow_id",
                    "mod_automation_run", ["workflow_id"])
    op.create_index("ix_mod_automation_run_user_id",
                    "mod_automation_run", ["user_id"])
    op.create_index("ix_mod_automation_run_status",
                    "mod_automation_run", ["status"])


def downgrade():
    op.drop_table("mod_automation_run")
    op.drop_table("mod_automation_workflow")
