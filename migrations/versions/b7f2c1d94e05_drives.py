"""drives (multi-drive support)

Revision ID: b7f2c1d94e05
Revises: 3afab4cdb2a7
Create Date: 2026-08-15 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7f2c1d94e05'
down_revision = '3afab4cdb2a7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('drives',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('drives', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_drives_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('folders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('drive_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_folders_drive_id'), ['drive_id'], unique=False)
        batch_op.create_foreign_key('fk_folders_drive_id', 'drives', ['drive_id'], ['id'])

    with op.batch_alter_table('files', schema=None) as batch_op:
        batch_op.add_column(sa.Column('drive_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_files_drive_id'), ['drive_id'], unique=False)
        batch_op.create_foreign_key('fk_files_drive_id', 'drives', ['drive_id'], ['id'])


def downgrade():
    with op.batch_alter_table('files', schema=None) as batch_op:
        batch_op.drop_constraint('fk_files_drive_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_files_drive_id'))
        batch_op.drop_column('drive_id')

    with op.batch_alter_table('folders', schema=None) as batch_op:
        batch_op.drop_constraint('fk_folders_drive_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_folders_drive_id'))
        batch_op.drop_column('drive_id')

    with op.batch_alter_table('drives', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_drives_user_id'))

    op.drop_table('drives')
