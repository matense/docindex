"""chat message model

Revision ID: f7a3b5c91e02
Revises: e5f1a2c38d44
Create Date: 2026-08-15 16:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7a3b5c91e02'
down_revision = 'e5f1a2c38d44'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model', sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.drop_column('model')
