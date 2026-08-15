"""drive description

Revision ID: c4e91a2b7f30
Revises: b7f2c1d94e05
Create Date: 2026-08-15 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4e91a2b7f30'
down_revision = 'b7f2c1d94e05'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('drives', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('description', sa.String(length=500), nullable=False,
                      server_default=''))


def downgrade():
    with op.batch_alter_table('drives', schema=None) as batch_op:
        batch_op.drop_column('description')
