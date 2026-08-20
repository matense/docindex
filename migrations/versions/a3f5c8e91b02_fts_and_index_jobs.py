"""fts5 search index and persistent index job queue

Adds two tables, neither touching existing data:
- file_fts: FTS5 virtual table (rowid = files.id) backfilled from the current
  files/file_index rows. Skipped gracefully if the SQLite build lacks FTS5 —
  search falls back to ILIKE in that case.
- index_jobs: persistent indexing queue (pending/running/done/error).

Revision ID: a3f5c8e91b02
Revises: 7a1c9e4b2d55
Create Date: 2026-08-19 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3f5c8e91b02'
down_revision = '7a1c9e4b2d55'
branch_labels = None
depends_on = None


def _fts5_available():
    conn = op.get_bind()
    try:
        conn.execute(sa.text("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)"))
        conn.execute(sa.text("DROP TABLE temp._fts5_probe"))
        return True
    except Exception:
        return False


def upgrade():
    op.create_table(
        'index_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['file_id'], ['files.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('index_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_index_jobs_file_id'), ['file_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_index_jobs_status'), ['status'], unique=False)

    if _fts5_available():
        op.execute(sa.text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS file_fts "
            "USING fts5(name, text, caption, tags)"))
        # Backfill from the current index (read-only on existing tables).
        op.execute(sa.text(
            "INSERT INTO file_fts(rowid, name, text, caption, tags) "
            "SELECT f.id, f.name, "
            "       COALESCE(fi.extracted_text, ''), "
            "       COALESCE(fi.caption, ''), "
            "       COALESCE(fi.hashtags, '') "
            "FROM files f LEFT JOIN file_index fi ON fi.file_id = f.id "
            "WHERE f.deleted_at IS NULL"))


def downgrade():
    op.execute(sa.text("DROP TABLE IF EXISTS file_fts"))
    with op.batch_alter_table('index_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_index_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_index_jobs_file_id'))
    op.drop_table('index_jobs')
