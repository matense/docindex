"""automation module: trigger file on runs

Runs started from a drive file (right-click → Run workflow) remember which
file triggered them, so the worker can build the trigger payload inside its
own app context.

Revision ID: automation_0002
Revises: automation_0001
Create Date: 2026-10-02 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'automation_0002'
down_revision = 'automation_0001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mod_automation_run",
                  sa.Column("trigger_file_id", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("mod_automation_run", "trigger_file_id")
