"""AI captions off by default (Drive.captions_enabled default False).

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-10-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("drives") as batch:
        batch.alter_column("captions_enabled", server_default=sa.false(),
                           existing_type=sa.Boolean(), existing_nullable=False)


def downgrade():
    with op.batch_alter_table("drives") as batch:
        batch.alter_column("captions_enabled", server_default=sa.true(),
                           existing_type=sa.Boolean(), existing_nullable=False)
