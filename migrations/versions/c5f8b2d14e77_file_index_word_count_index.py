"""index on file_index.word_count

The advanced agent search filters and sorts by word_count — an index keeps
those range scans fast over large libraries.

Revision ID: c5f8b2d14e77
Revises: b4e7a1c90d33
Create Date: 2026-09-06 10:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'c5f8b2d14e77'
down_revision = 'b4e7a1c90d33'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_file_index_word_count", "file_index", ["word_count"])


def downgrade():
    op.drop_index("ix_file_index_word_count", table_name="file_index")
