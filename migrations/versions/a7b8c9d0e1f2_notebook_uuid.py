"""Internal UUID for notebooks (files.uuid, globally unique).

Existing notebooks have NULL and are backfilled lazily on next access
(see doc.ensure_uuid); the column is nullable so non-notebook files are
unaffected (multiple NULLs are allowed by the unique constraint).

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-20 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("files") as batch:
        batch.add_column(sa.Column("uuid", sa.String(36), nullable=True))
        batch.create_unique_constraint("uq_files_uuid", ["uuid"])
        batch.create_index("ix_files_uuid", ["uuid"])


def downgrade():
    with op.batch_alter_table("files") as batch:
        batch.drop_index("ix_files_uuid")
        batch.drop_constraint("uq_files_uuid", type_="unique")
        batch.drop_column("uuid")
