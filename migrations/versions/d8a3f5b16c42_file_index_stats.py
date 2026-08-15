"""file index statistics (word/line/char counts)

Revision ID: d8a3f5b16c42
Revises: c4e91a2b7f30
Create Date: 2026-08-15 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8a3f5b16c42'
down_revision = 'c4e91a2b7f30'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('file_index', schema=None) as batch_op:
        batch_op.add_column(sa.Column('word_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('line_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('char_count', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('file_index', schema=None) as batch_op:
        batch_op.drop_column('char_count')
        batch_op.drop_column('line_count')
        batch_op.drop_column('word_count')
