"""folder trash (deleted_at)

Revision ID: 7d3f1a9b2c4e
Revises: f1e2d3c4b5a6
Create Date: 2026-11-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7d3f1a9b2c4e'
down_revision = 'f1e2d3c4b5a6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('folders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_folders_deleted_at'), ['deleted_at'], unique=False)


def downgrade():
    with op.batch_alter_table('folders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_folders_deleted_at'))
        batch_op.drop_column('deleted_at')
