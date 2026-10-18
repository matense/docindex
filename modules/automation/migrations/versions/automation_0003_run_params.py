"""automation module: run parameters on runs

Runs started manually may carry answers to input.param nodes (text,
questions, files) collected in the run form; the worker exposes them to
executors as ctx["params"].

Revision ID: automation_0003
Revises: automation_0002
Create Date: 2026-10-04 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'automation_0003'
down_revision = 'automation_0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mod_automation_run",
                  sa.Column("params", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("mod_automation_run", "params")
