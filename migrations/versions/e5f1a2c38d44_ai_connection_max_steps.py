"""ai connection max steps

Revision ID: e5f1a2c38d44
Revises: d8a3f5b16c42
Create Date: 2026-08-15 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f1a2c38d44'
down_revision = 'd8a3f5b16c42'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('ai_connections', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_steps', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('ai_connections', schema=None) as batch_op:
        batch_op.drop_column('max_steps')
