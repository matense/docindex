"""Git drives: stored LFS badge scan (lfs_total/sample/scanned_at).

Revision ID: 9c0d1e2f3a4b
Revises: f4a5b6c7d8e9
Create Date: 2026-11-19 00:00:00.000000

The advisory LFS badge used to be recomputed by walking the checkout on
every list-page view (with a TTL-cache band-aid). It is now computed once
in the sync worker (pre_sync) after each successful materialization and
stored on the drive row; the page reads these columns and never touches
the filesystem.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '9c0d1e2f3a4b'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("drives") as batch:
        batch.add_column(sa.Column("lfs_total", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("lfs_sample", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("lfs_scanned_at", sa.DateTime(),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table("drives") as batch:
        batch.drop_column("lfs_scanned_at")
        batch.drop_column("lfs_sample")
        batch.drop_column("lfs_total")
