"""Keep captions_enabled default true; only the creation UIs default to off.

The previous migration set a false server default; the application always
writes the value explicitly (sync modal unchecked by default, git drives
created with captions off), so restore the original server default.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f2a3b4c5d6e7'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("drives") as batch:
        batch.alter_column("captions_enabled", server_default=sa.true(),
                           existing_type=sa.Boolean(), existing_nullable=False)


def downgrade():
    with op.batch_alter_table("drives") as batch:
        batch.alter_column("captions_enabled", server_default=sa.false(),
                           existing_type=sa.Boolean(), existing_nullable=False)
