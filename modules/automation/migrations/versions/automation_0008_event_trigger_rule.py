"""automation module: event trigger rule on workflows

Event-driven runs: AutomationWorkflow.trigger_event is a nullable JSON rule
{"drive_id"?, "folder"?, "pattern"?} — when set (and the workflow is
enabled), matching file.uploaded / file.synced bus events start a run with
trigger="event". Missing keys match everything within the event's scope.

Revision ID: automation_0008
Revises: automation_0007
Create Date: 2026-11-18 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'automation_0008'
down_revision = 'automation_0007'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mod_automation_workflow",
                  sa.Column("trigger_event", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("mod_automation_workflow", "trigger_event")
