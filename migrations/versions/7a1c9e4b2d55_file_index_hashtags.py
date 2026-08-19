"""file index hashtags

Revision ID: 7a1c9e4b2d55
Revises: 00efe0293465
Create Date: 2026-08-19 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7a1c9e4b2d55'
down_revision = '00efe0293465'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('file_index', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hashtags', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('file_index', schema=None) as batch_op:
        batch_op.drop_column('hashtags')
