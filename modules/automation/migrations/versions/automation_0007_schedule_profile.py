"""automation module: schedule profiles on workflows

Scheduled runs have nobody to answer the run form, so the workflow stores
frozen answers (schedule_params JSON {params, file_id}) that the scheduler
replays every tick. schedule_file_id is the trigger file for schedules
of trigger workflows.

Revision ID: automation_0007
Revises: automation_0006
Create Date: 2026-10-31 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'automation_0007'
down_revision = 'automation_0006'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mod_automation_workflow",
                  sa.Column("schedule_params", sa.Text(), nullable=True))
    op.add_column("mod_automation_workflow",
                  sa.Column("schedule_file_id", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("mod_automation_workflow", "schedule_file_id")
    op.drop_column("mod_automation_workflow", "schedule_params")
