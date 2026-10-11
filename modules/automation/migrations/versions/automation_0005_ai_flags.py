"""automation module: AI access flags on workflows

ai_lock blocks AI edits (edit_workflow refuses); ai_hidden hides the
workflow from the AI entirely (list/get/edit/run tools refuse it) — the
same lock/visibility pattern as notebooks.

Revision ID: automation_0005
Revises: automation_0004
Create Date: 2026-10-09 21:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'automation_0005'
down_revision = 'automation_0004'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("mod_automation_workflow") as batch:
        batch.add_column(sa.Column("ai_lock", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.add_column(sa.Column("ai_hidden", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("mod_automation_workflow") as batch:
        batch.drop_column("ai_hidden")
        batch.drop_column("ai_lock")
